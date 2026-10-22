#!/usr/bin/env python3
"""vyce-cloud-tool —— vyce.cloud 免费 Vps-Mini 名额监听 / 开抢工具（M0）

只做三件事：
  1. 轮询公开接口 /api/stock，盯着免费名额是否放出；
  2. 放出时（可选）用你自己的会话 Cookie 下单，只开一台；
  3. 把每次状态翻转记到 data/stock.jsonl，供事后统计放号规律。

设计边界（见 docs/01-立项.md）：
  * 单账号、单实例，不提供多开/代注册；
  * 不自动续期（续期按钮带广告，跳过即刷量）；
  * 轮询默认 2s（约 30 次/分钟，是 300 次/分钟限流的 10%）。

仅用 Python 3.8+ 标准库。
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG_PATH = ROOT / "config.json"
SESSION_PATH = ROOT / "session.txt"
STOCK_LOG = DATA_DIR / "stock.jsonl"
LAST_SERVER = DATA_DIR / "last-server.json"

TZ = timezone(timedelta(hours=8))
UA = "vyce-cloud-tool/0.1 (personal use, single account)"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,22}[a-z0-9]$")

# 站点 POST /api/servers 的 os 字段吃的是展示名（实测 400 才发现的坑），
# 前端 state 用的也是 Kg[].name。这里做 id → 展示名映射，两者都接受。
OS_OPTIONS = {
    "ubuntu-24": "Ubuntu 24.04",
    "ubuntu-22": "Ubuntu 22.04",
    "debian-12": "Debian 12",
    "fedora-41": "Fedora 41",
    "alma-9": "AlmaLinux 9",
}


def resolve_os(value: str) -> str:
    if value in OS_OPTIONS:
        return OS_OPTIONS[value]
    if value in OS_OPTIONS.values():
        return value
    die(f"os 不支持：{value}（可选 {', '.join(OS_OPTIONS)}）")

DEFAULTS = {
    "base_url": "https://vyce.cloud",
    "poll_interval": 2.0,
    "min_request_interval": 1.0,
    "server_name": "vyce-mini",
    "os": "ubuntu-24",
    "region_preference": ["Europe", "Asia", "USA"],
    "key_ids": [],
    "auto_deploy": False,
    "max_deploy_wait_seconds": 600,
}


# ---------------------------------------------------------------- 基础工具

def now() -> datetime:
    return datetime.now(TZ)


def ts(dt: datetime | None = None) -> str:
    return (dt or now()).strftime("%Y-%m-%d %H:%M:%S")


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            die(f"config.json 解析失败：{exc}")
    return cfg


def load_cookie() -> str:
    raw = ""
    if SESSION_PATH.exists():
        raw = SESSION_PATH.read_text(encoding="utf-8", errors="replace").strip()
    if not raw:
        raw = (__import__("os").environ.get("VYCE_COOKIE") or "").strip()
    raw = raw.replace("\r", "").replace("\n", "")
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    return raw


def die(msg: str, code: int = 1) -> None:
    print(f"[x] {msg}", file=sys.stderr)
    raise SystemExit(code)


def ok(msg: str) -> None:
    print(f"[+] {msg}", flush=True)


def info(msg: str) -> None:
    print(f"[·] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[!] {msg}", flush=True)


# ---------------------------------------------------------------- HTTP 客户端

class ApiError(Exception):
    def __init__(self, status: int, payload: dict):
        self.status = status
        self.payload = payload or {}
        msg = self.payload.get("message") or self.payload.get("error") or f"HTTP {status}"
        super().__init__(msg)


class Api:
    """带令牌桶的最小 HTTP 客户端：遵守 x-ratelimit-*，429/5xx 退避。"""

    def __init__(self, base_url: str, cookie: str = "", min_interval: float = 1.0, timeout: int = 15):
        self.base = base_url.rstrip("/")
        self.cookie = cookie
        self.min_interval = max(0.5, float(min_interval))
        self.timeout = timeout
        self._last = 0.0

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last
        wait = self.min_interval - gap
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.1))
        self._last = time.monotonic()

    def request(self, method: str, path: str, body: dict | None = None,
                retries: int = 2) -> tuple[dict, dict]:
        for attempt in range(retries + 1):
            self._throttle()
            req = urllib.request.Request(self.base + path, method=method)
            req.add_header("User-Agent", UA)
            req.add_header("Accept", "application/json")
            # 站点的 CSRF 校验要求同源：带 Origin/Referer 才能通过 POST
            req.add_header("Origin", self.base)
            req.add_header("Referer", self.base + "/")
            if self.cookie:
                req.add_header("Cookie", self.cookie)
            if body is not None:
                req.data = json.dumps(body).encode("utf-8")
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    headers = {k.lower(): v for k, v in resp.headers.items()}
                    try:
                        payload = json.loads(raw or b"{}")
                    except json.JSONDecodeError:
                        payload = {"raw": raw[:300].decode("utf-8", "replace")}
                    return payload, headers
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                try:
                    payload = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    payload = {"raw": raw[:300].decode("utf-8", "replace")}
                if exc.code == 429 and attempt < retries:
                    delay = _retry_delay(exc.headers)
                    warn(f"触发限流 429，退避 {delay:.0f}s")
                    time.sleep(delay)
                    continue
                if exc.code >= 500 and attempt < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise ApiError(exc.code, payload)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise ApiError(0, {"message": f"网络不可达：{exc}"})
        raise ApiError(0, {"message": "请求重试耗尽"})


def _retry_delay(headers) -> float:
    try:
        reset = float(headers.get("x-ratelimit-reset") or 0)
        if reset > 0:
            return min(60.0, reset + 1)
    except (TypeError, ValueError):
        pass
    return 5.0


# ---------------------------------------------------------------- 业务读取

def fetch_stock(api: Api) -> dict:
    payload, _ = api.request("GET", "/api/stock")
    free = payload.get("free") or {}
    regions = {}
    for item in payload.get("regions") or []:
        rid = item.get("id")
        if rid:
            regions[rid] = {
                "freeOpen": bool(item.get("freeOpen")),
                "open": bool(item.get("open")),
            }
    return {
        "open": bool(free.get("open")),
        "enabled": bool(free.get("enabled")),
        "regions": regions,
    }


def free_regions(snap: dict, preference: list[str]) -> list[str]:
    order = list(preference) + [r for r in snap["regions"] if r not in preference]
    return [r for r in order if snap["regions"].get(r, {}).get("freeOpen")]


def fetch_me(api: Api) -> dict:
    payload, _ = api.request("GET", "/api/me")
    return payload.get("user") or {}


def fetch_servers(api: Api) -> list[dict]:
    payload, _ = api.request("GET", "/api/servers")
    return payload.get("servers") or []


def fetch_keys(api: Api) -> list[dict]:
    payload, _ = api.request("GET", "/api/keys")
    return payload.get("keys") or []


def append_log(record: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with STOCK_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 开抢

def deploy(api: Api, cfg: dict, snap: dict, dry_run: bool) -> dict | None:
    """在确认有名额后尝试下单。返回服务器信息或 None。"""
    me = fetch_me(api)
    servers = fetch_servers(api)
    mine = [s for s in servers if s.get("source") == "free"]
    if mine:
        warn(f"账号已有免费机 {mine[0].get('name') or mine[0].get('id')}，按 ToS 一人一台，不再下单")
        return None

    keys = fetch_keys(api)
    key_ids = [k["id"] for k in keys if k.get("id") is not None]
    wanted = [str(k) for k in (cfg.get("key_ids") or [])]
    if wanted:
        filtered = [k for k in key_ids if str(k) in wanted]
        if filtered:
            key_ids = filtered
        else:
            warn(f"config.key_ids={wanted} 在账号里不存在，改用全部已登记 key")
    if not key_ids:
        warn("账号没有登记 SSH key，无法建机：请先跑 `key-add`")
        return None

    candidates = free_regions(snap, cfg.get("region_preference") or [])
    if not candidates:
        if dry_run:
            candidates = list(cfg.get("region_preference") or list(snap["regions"]))[:1]
            warn(f"当前无可用免费区域，dry-run 用首个偏好区域 {candidates} 演练")
        else:
            warn("stock.open 为真但所有区域 freeOpen 均为假，跳过本轮")
            return None
    region = candidates[0]

    name = str(cfg.get("server_name") or "vyce-mini").lower()
    if not NAME_RE.match(name):
        die(f"server_name 不合法（需 3–24 位小写字母/数字/连字符）：{name}")

    body = {
        "name": name,
        "os": resolve_os(cfg.get("os") or "ubuntu-24"),
        "region": region,
        "planId": "mini",
        "keyIds": key_ids,
    }
    info(f"准备下单：{json.dumps(body, ensure_ascii=False)}")
    if dry_run:
        ok("dry-run：未真正提交 POST /api/servers")
        return None

    try:
        created, _ = api.request("POST", "/api/servers", body)
    except ApiError as exc:
        if exc.status == 503 or exc.payload.get("error") == "no_stock":
            warn(f"没抢到：{exc}（名额在提交瞬间被拿走），继续监听")
            return None
        if exc.status == 409:
            warn(f"没抢到（{exc}）——名额已被别人拿走，继续监听")
            return None
        if exc.status == 401:
            die("会话已失效，请重新导入 session.txt 里的 Cookie")
        if exc.status == 400:
            die(f"请求被拒：{exc} {exc.payload.get('fields') or ''}")
        raise

    sid = created.get("id")
    info(f"下单返回 id={sid}，等待建机完成……")
    result = wait_for_server(api, sid, cfg.get("max_deploy_wait_seconds", 600))
    record = {
        "created_at": ts(),
        "id": sid,
        "name": body["name"],
        "region": region,
        "os": body["os"],
        "status": result.get("status"),
        "connect": result.get("connect") or created.get("connect"),
        "ipv6": result.get("ipv6") or created.get("ipv6"),
        "credentials": result.get("credentials") or created.get("credentials"),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LAST_SERVER.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    conn = record.get("connect") or {}
    if conn.get("host"):
        ok(f"建机成功：ssh -p {conn.get('sshPort')} root@{conn.get('host')}")
    elif record.get("ipv6"):
        ok(f"建机成功：ssh root@{record['ipv6']}")
    else:
        ok(f"建机成功，详情见 {LAST_SERVER}")
    print("\a", end="", flush=True)
    return record


def wait_for_server(api: Api, sid, max_wait: int) -> dict:
    deadline = time.monotonic() + max_wait
    last = {}
    while time.monotonic() < deadline:
        time.sleep(3)
        try:
            payload, _ = api.request("GET", f"/api/servers/{sid}")
        except ApiError as exc:
            if exc.status == 404:
                return {"status": "not-found"}
            warn(f"查询建机状态失败：{exc}")
            continue
        last = payload.get("server") or payload
        state = str(last.get("status") or "")
        if state in ("running", "ready", "active"):
            return last
        if state in ("error", "failed"):
            warn(f"建机失败：{last.get('error') or state}")
            return last
        print(f"\r[·] 建机中…… {state or 'creating'}   ", end="", flush=True)
    print()
    warn(f"等待建机超时（{max_wait}s），请到面板确认")
    return last


# ---------------------------------------------------------------- 子命令

def cmd_status(api: Api, cfg: dict) -> int:
    snap = fetch_stock(api)
    print(f"时间       : {ts()}")
    print(f"免费名额   : {'开放 ✅' if snap['open'] else '未开放'}"
          f"（enabled={snap['enabled']}）")
    print("区域       :")
    for rid, st in snap["regions"].items():
        flag = "free=开 " if st["freeOpen"] else "free=关 "
        print(f"  - {rid:<7} {flag} (region.open={st['open']})")
    cands = free_regions(snap, cfg.get("region_preference") or [])
    if cands:
        print(f"可抢区域   : {', '.join(cands)}")
    if SESSION_PATH.exists() or load_cookie():
        try:
            me = fetch_me(api)
            servers = fetch_servers(api)
            free = [s for s in servers if s.get("source") == "free"]
            print(f"登录身份   : {me.get('username') or me.get('login') or '?'} "
                  f"(免费机 {len(free)} 台 / 共 {len(servers)} 台)")
        except ApiError as exc:
            print(f"登录身份   : 会话无效（{exc}）—— 重新导出 Cookie 到 session.txt")
    else:
        print("登录身份   : 未配置 session.txt（只读监听仍可用）")
    return 0


def cmd_watch(api: Api, cfg: dict, args) -> int:
    interval = max(1.0, float(args.interval or cfg["poll_interval"]))
    cookie = load_cookie()
    auto = bool(args.deploy) and bool(cookie) and not args.dry_run
    if args.deploy and not cookie:
        warn("没有 session.txt，无法下单；本次退化为只读监听")
    if auto:
        me = fetch_me(api)
        ok(f"以 {me.get('username') or me.get('login')} 身份监听；检测到名额将自动下单")
    else:
        info("只读监听模式（不会下单）")

    print(f"[·] 轮询间隔 {interval:.1f}s，Ctrl+C 结束")
    prev = None
    started = time.monotonic()
    last_beat = started
    checks = 0
    try:
        while True:
            if args.seconds and time.monotonic() - started >= args.seconds:
                break
            t0 = time.monotonic()
            try:
                snap = fetch_stock(api)
            except ApiError as exc:
                warn(f"取库存失败：{exc}")
                time.sleep(min(30.0, interval * 3))
                continue
            latency = (time.monotonic() - t0) * 1000
            checks += 1
            cands = free_regions(snap, cfg.get("region_preference") or [])

            if prev is None:
                state = "开放" if snap["open"] else "关闭"
                info(f"当前状态：{state}｜区域 {', '.join(snap['regions']) or '-'}｜{latency:.0f}ms")
            elif snap["open"] != prev["open"] or cands != prev["cands"]:
                append_log({
                    "ts": ts(),
                    "epoch": int(time.time()),
                    "open": snap["open"],
                    "regions": snap["regions"],
                    "free_regions": cands,
                    "latency_ms": round(latency),
                })
                if snap["open"] and cands:
                    ok(f"名额开放！区域：{', '.join(cands)}（{latency:.0f}ms）")
                    if auto:
                        try:
                            deploy(api, cfg, snap, dry_run=False)
                        except ApiError as exc:
                            warn(f"下单异常：{exc}")
                        prev = {"open": snap["open"], "cands": cands}
                        info("已尝试下单，继续监听")
                        time.sleep(interval)
                        continue
                    print("\a", end="", flush=True)
                else:
                    info(f"名额关闭（区域 {', '.join(cands) or '-'}）")

            prev = {"open": snap["open"], "cands": cands}

            if time.monotonic() - last_beat > 300:
                last_beat = time.monotonic()
                info(f"心跳：{checks} 次轮询，当前{'开放' if snap['open'] else '关闭'}，"
                     f"最近延迟 {latency:.0f}ms")

            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, interval - elapsed) + random.uniform(0, 0.15))
    except KeyboardInterrupt:
        info("已停止")
    if checks:
        info(f"本轮共轮询 {checks} 次")
    return 0


def cmd_auth_check(api: Api, cfg: dict) -> int:
    cookie = load_cookie()
    if not cookie:
        die("未找到 session.txt（把浏览器里请求头的 Cookie 整行粘进去）")
    me = fetch_me(api)
    servers = fetch_servers(api)
    keys = fetch_keys(api)
    free = [s for s in servers if s.get("source") == "free"]
    age = me.get("githubCreatedAt")
    print(f"账号       : {me.get('username') or me.get('login')}")
    if isinstance(age, (int, float)):
        days = int((time.time() * 1000 - age) / 86400000)
        print(f"GitHub 龄  : {days} 天" + ("" if days >= 30 else "  ⚠ 不足 30 天，无法建机"))
    else:
        print(f"GitHub 龄  : {age or '?'}")
    print(f"服务器     : {len(servers)} 台（免费 {len(free)} 台）")
    print(f"SSH keys   : {len(keys)} 个")
    for k in keys:
        print(f"  - #{k.get('id')} {k.get('name')}")
    if not keys:
        warn("没有 SSH key，建机前请先跑 `key-add`")
    if free:
        info("已有免费机：本工具不会重复下单")
    return 0


def cmd_key_add(api: Api, cfg: dict, args) -> int:
    pub_path = Path(args.pub).expanduser()
    if not pub_path.exists():
        die(f"公钥文件不存在：{pub_path}")
    key = pub_path.read_text(encoding="utf-8", errors="replace").strip()
    if not re.match(r"^(ssh-(rsa|ed25519)|ecdsa-sha2-)", key):
        die(f"不像是 OpenSSH 公钥：{pub_path}")
    name = args.name or pub_path.stem
    existing = {k.get("name") for k in fetch_keys(api)}
    if name in existing:
        info(f"账号已有同名 key「{name}」，跳过")
        return 0
    payload, _ = api.request("POST", "/api/keys", {"name": name, "key": key})
    kid = (payload.get("key") or {}).get("id") or payload.get("id")
    ok(f"已登记公钥「{name}」id={kid}")
    return 0


def cmd_deploy(api: Api, cfg: dict, args) -> int:
    cookie = load_cookie()
    if not cookie:
        die("需要 session.txt 才能下单")
    snap = fetch_stock(api)
    if not snap["open"] and not args.dry_run:
        warn("当前免费名额未开放，先跑 `watch`")
        return 2
    if not snap["open"]:
        warn("名额未开放；本次只做前置检查（dry-run 不会提交）")
    result = deploy(api, cfg, snap, dry_run=args.dry_run)
    return 0 if result else 3


def cmd_stats(cfg: dict) -> int:
    if not STOCK_LOG.exists():
        info("还没有 data/stock.jsonl，先跑一段时间 watch")
        return 0
    rows = []
    for line in STOCK_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not rows:
        info("日志为空")
        return 0
    opens = [r for r in rows if r.get("open")]
    print(f"记录条数   : {len(rows)}（其中开放 {len(opens)} 次）")
    by_hour: dict[int, int] = {}
    for r in opens:
        try:
            hour = datetime.fromisoformat(r["ts"]).hour
        except (KeyError, ValueError):
            continue
        by_hour[hour] = by_hour.get(hour, 0) + 1
    if by_hour:
        print("放号时段   :")
        for hour in sorted(by_hour):
            print(f"  {hour:02d}:00–{hour:02d}:59  {'█' * by_hour[hour]} {by_hour[hour]}")
    print("最近 10 次 :")
    for r in rows[-10:]:
        print(f"  {r.get('ts')}  {'OPEN ' if r.get('open') else 'close'}  "
              f"{','.join(r.get('free_regions') or []) or '-'}")
    return 0


# ---------------------------------------------------------------- 入口

def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    parser = argparse.ArgumentParser(
        prog="vyce.py",
        description="vyce.cloud 免费 Vps-Mini 名额监听/开抢（个人单账号用）",
    )
    parser.add_argument("--config", help="配置文件路径（默认项目根 config.json）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="看一次当前名额与账号状态")
    p_watch = sub.add_parser("watch", help="持续轮询，名额放出时告警/下单")
    p_watch.add_argument("--interval", type=float, help="轮询间隔秒（默认取配置 2.0）")
    p_watch.add_argument("--seconds", type=int, default=0, help="跑多少秒后退出，0=一直跑")
    p_watch.add_argument("--deploy", action="store_true", help="检测到名额时自动下单")
    p_watch.add_argument("--dry-run", action="store_true", help="即使 --deploy 也不真正提交")
    sub.add_parser("auth-check", help="验证 session.txt 里的 Cookie 是否有效")
    p_key = sub.add_parser("key-add", help="把本地公钥登记到账号（建机前置）")
    p_key.add_argument("--pub", default="~/.ssh/id_ed25519.pub", help="公钥路径")
    p_key.add_argument("--name", help="key 名称（默认取文件名）")
    p_dep = sub.add_parser("deploy", help="名额已开放时手动下单一次")
    p_dep.add_argument("--dry-run", action="store_true", help="只打印将要提交的内容")
    sub.add_parser("stats", help="统计 data/stock.jsonl 里的放号规律")

    args = parser.parse_args(argv)

    global CONFIG_PATH
    if args.config:
        CONFIG_PATH = Path(args.config).resolve()
    cfg = load_config()

    if args.cmd == "stats":
        return cmd_stats(cfg)

    api = Api(cfg["base_url"], load_cookie(),
              min_interval=cfg.get("min_request_interval", 1.0))

    try:
        if args.cmd == "status":
            return cmd_status(api, cfg)
        if args.cmd == "watch":
            return cmd_watch(api, cfg, args)
        if args.cmd == "auth-check":
            return cmd_auth_check(api, cfg)
        if args.cmd == "key-add":
            return cmd_key_add(api, cfg, args)
        if args.cmd == "deploy":
            return cmd_deploy(api, cfg, args)
    except ApiError as exc:
        die(f"{exc}（HTTP {exc.status}）{json.dumps(exc.payload, ensure_ascii=False)[:300]}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
