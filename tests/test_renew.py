"""续期逻辑离线验证：状态机与前端 Hy() 对齐、429 冷却、回读校验。

不联网：用 FakeApi 顶掉 HTTP 层。
用法：python -m unittest discover -s tests -v
"""
import importlib.util
import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "vyce.py"
spec = importlib.util.spec_from_file_location("vyce", SRC)
vyce = importlib.util.module_from_spec(spec)
sys.modules["vyce"] = vyce
spec.loader.exec_module(vyce)

DAY = 86400000
HOUR = 3600000


class FakeApi:
    """按 (method, path) 返回脚本化响应；记录调用顺序。"""

    def __init__(self, servers_seq, renew_result=None):
        self.servers_seq = list(servers_seq)
        self.renew_result = renew_result or (200, {"success": True})
        self.calls = []

    def request(self, method, path, body=None, retries=2):
        self.calls.append((method, path))
        if method == "GET" and path == "/api/servers":
            servers = self.servers_seq.pop(0) if len(self.servers_seq) > 1 else self.servers_seq[0]
            return {"servers": servers}, {}
        if method == "POST" and path.endswith("/renew"):
            status, payload = self.renew_result
            if status >= 400:
                raise vyce.ApiError(status, payload)
            return payload, {}
        raise AssertionError(f"unexpected call {method} {path}")


def free_server(expires_in_ms, status="running", **extra):
    now = int(time.time() * 1000)
    srv = {"id": "srv-1", "name": "vyce-mini", "source": "free", "planId": "mini",
           "status": status, "expiresAt": now + expires_in_ms}
    srv.update(extra)
    return srv


class RenewStateTest(unittest.TestCase):
    """状态机必须与站点前端 Hy() 一致（cooldown 阈值 1 天，soon 阈值 6 小时）。"""

    def test_fresh_server_is_cooldown(self):
        st = vyce.renew_state(free_server(47 * HOUR), int(time.time() * 1000))
        self.assertEqual(st["state"], "cooldown")
        self.assertFalse(st["canRenew"])
        self.assertAlmostEqual(st["cooldownLeft"] / HOUR, 23, delta=0.1)

    def test_exactly_24h_left_is_renewable(self):
        st = vyce.renew_state(free_server(24 * HOUR - 60_000), int(time.time() * 1000))
        self.assertEqual(st["state"], "ok")
        self.assertTrue(st["canRenew"])

    def test_soon_window(self):
        st = vyce.renew_state(free_server(3 * HOUR), int(time.time() * 1000))
        self.assertEqual(st["state"], "soon")
        self.assertTrue(st["canRenew"])

    def test_late(self):
        st = vyce.renew_state(free_server(-2 * HOUR), int(time.time() * 1000))
        self.assertEqual(st["state"], "late")
        self.assertTrue(st["canRenew"])

    def test_stopped_is_renewable(self):
        st = vyce.renew_state(free_server(47 * HOUR, status="stopped"), int(time.time() * 1000))
        self.assertEqual(st["state"], "stopped")
        self.assertTrue(st["canRenew"])

    def test_non_free_and_suspended_are_none(self):
        now = int(time.time() * 1000)
        self.assertEqual(vyce.renew_state({**free_server(47 * HOUR), "source": "paid"}, now)["state"], "none")
        self.assertEqual(vyce.renew_state(free_server(47 * HOUR, suspended=True), now)["state"], "none")
        self.assertEqual(vyce.renew_state(free_server(47 * HOUR, status="creating"), now)["state"], "none")


class RenewOnceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vyce-renew-"))
        self._old = (vyce.DATA_DIR, vyce.RENEW_STATE)
        vyce.DATA_DIR = self.tmp
        vyce.RENEW_STATE = self.tmp / "renew_state.json"

    def tearDown(self):
        vyce.DATA_DIR, vyce.RENEW_STATE = self._old

    def _run(self, api, **kw):
        buf = io.StringIO()
        with redirect_stdout(buf):
            failures = vyce.renew_once(api, {}, **kw)
        return failures, buf.getvalue()

    def test_cooldown_skips_request(self):
        api = FakeApi([[free_server(47 * HOUR)]])
        failures, out = self._run(api)
        self.assertEqual(failures, 0)
        self.assertIn("冷却中", out)
        self.assertFalse(any(m == "POST" for m, _ in api.calls), "冷却期不应发续期请求")
        state = json.loads(vyce.RENEW_STATE.read_text(encoding="utf-8"))
        self.assertEqual(state["srv-1"]["status"], "cooldown")
        self.assertIn("next_renewal_ts", state["srv-1"])

    def test_renew_success_requires_expiry_advance(self):
        old = free_server(3 * HOUR)
        new = dict(old, expiresAt=old["expiresAt"] + 45 * HOUR)
        api = FakeApi([[old], [new]])
        failures, out = self._run(api)
        self.assertEqual(failures, 0)
        self.assertIn("续期成功", out)
        state = json.loads(vyce.RENEW_STATE.read_text(encoding="utf-8"))
        self.assertEqual(state["srv-1"]["status"], "success")
        self.assertGreater(state["srv-1"]["expiresAt"], old["expiresAt"])

    def test_200_without_advance_is_flagged(self):
        old = free_server(3 * HOUR)
        api = FakeApi([[old], [dict(old)]])   # 到期时间没变
        failures, out = self._run(api)
        self.assertEqual(failures, 1)
        self.assertIn("到期时间未推进", out)
        state = json.loads(vyce.RENEW_STATE.read_text(encoding="utf-8"))
        self.assertEqual(state["srv-1"]["status"], "renewed_unverified")

    def test_429_cooldown_is_not_failure(self):
        api = FakeApi([[free_server(3 * HOUR)]], renew_result=(
            429, {"error": "renewal_cooldown",
                  "message": "Please wait 5 more hours before renewing again."}))
        failures, out = self._run(api)
        self.assertEqual(failures, 0, "429 冷却属于预期，不算失败")
        self.assertIn("冷却", out)
        state = json.loads(vyce.RENEW_STATE.read_text(encoding="utf-8"))
        self.assertEqual(state["srv-1"]["status"], "cooldown_429")
        wait_h = (state["srv-1"]["next_renewal_ts"] - int(time.time() * 1000)) / HOUR
        self.assertAlmostEqual(wait_h, 5, delta=0.1)

    def test_404_removes_record(self):
        api = FakeApi([[free_server(3 * HOUR)]],
                      renew_result=(404, {"error": "not_found", "message": "Server not found."}))
        failures, out = self._run(api)
        self.assertEqual(failures, 0)
        self.assertIn("已不存在", out)

    def test_dry_run_sends_nothing(self):
        api = FakeApi([[free_server(3 * HOUR)]])
        failures, out = self._run(api, dry_run=True)
        self.assertEqual(failures, 0)
        self.assertIn("dry-run", out)
        self.assertFalse(any(m == "POST" for m, _ in api.calls))

    def test_force_bypasses_cooldown(self):
        old = free_server(47 * HOUR)
        api = FakeApi([[old], [dict(old, expiresAt=old["expiresAt"] + HOUR)]])
        failures, out = self._run(api, force=True)
        self.assertTrue(any(m == "POST" for m, _ in api.calls), "--force 应真的提交")

    def test_no_free_server_is_noop(self):
        api = FakeApi([[]])
        failures, out = self._run(api)
        self.assertEqual(failures, 0)
        self.assertIn("没有免费服务器", out)


class CooldownDetectTest(unittest.TestCase):
    def test_detection(self):
        self.assertTrue(vyce.is_cooldown_error(429, "renewal_cooldown"))
        self.assertTrue(vyce.is_cooldown_error(429, "Please wait 3 more hours"))
        self.assertFalse(vyce.is_cooldown_error(429, "rate limited"))
        self.assertFalse(vyce.is_cooldown_error(500, "renewal_cooldown"))


class CaptchaDetectTest(unittest.TestCase):
    """2026-10 起 /renew 需要 BestCap 的 captchaResponse，必须与冷却/限流区分开。"""

    def test_detection(self):
        self.assertTrue(vyce.is_captcha_error(400, "captcha_required"))
        self.assertTrue(vyce.is_captcha_error(400, "Invalid CAPTCHA response"))
        self.assertTrue(vyce.is_captcha_error(403, "verification failed"))
        self.assertTrue(vyce.is_captcha_error(422, "bestcap token expired"))

    def test_not_confused_with_cooldown_or_limit(self):
        self.assertFalse(vyce.is_captcha_error(429, "renewal_cooldown"))
        self.assertFalse(vyce.is_captcha_error(429, "rate limited"))
        self.assertFalse(vyce.is_captcha_error(400, "Invalid UUID"))
        self.assertFalse(vyce.is_captcha_error(500, "captcha"))

    def test_renew_explains_when_solver_disabled(self):
        old = free_server(47 * HOUR)

        class CapApi(FakeApi):
            def request(self, method, path, body=None, retries=2):
                self.calls.append((method, path))
                if method == "GET" and path == "/api/servers":
                    return {"servers": [old]}, {}
                raise vyce.ApiError(400, {"error": "captcha_required",
                                          "message": "Please complete the CAPTCHA verification first."})

        api = CapApi([[]])
        # captcha_solver=False 时必须立刻停手，绝不允许测试去启动真实浏览器
        failures, out = vyce_capture(api, {"captcha_solver": False})
        self.assertEqual(failures, 1)
        self.assertIn("验证码", out)
        self.assertIn("面板手动", out)
        self.assertIn("captcha_solver=false", out)


class UnblockRoutingTest(unittest.TestCase):
    """验证码拦住后走哪条免人工路径：停机/过期 → /start；运行中 → 浏览器求解。"""

    def setUp(self):
        self._old = vyce.solve_captcha_and_renew
        self.calls = []

    def tearDown(self):
        vyce.solve_captcha_and_renew = self._old

    def _fake_solver(self, ok, detail="solver-ran"):
        def fake(api, cfg, sid, cookie):
            self.calls.append(("solver", sid))
            return ok, detail
        vyce.solve_captcha_and_renew = fake

    def _api(self):
        holder = {}

        class Rec(FakeApi):
            def request(self, method, path, body=None, retries=2):
                holder["last"] = (method, path, body)
                return {}, {}

        return Rec([[]]), holder

    def test_stopped_server_uses_start_not_solver(self):
        self._fake_solver(True)
        api, holder = self._api()
        how, detail = vyce.unblock_renewal(api, {}, "srv-1", "stopped", "c=1")
        self.assertEqual(how, "start")
        self.assertEqual(holder["last"][:2], ("POST", "/api/servers/srv-1/start"))
        self.assertEqual(holder["last"][2], {}, "power action 的 body 必须是 {}")
        self.assertFalse([c for c in self.calls if c[0] == "solver"], "不该白跑一次浏览器")

    def test_expired_server_uses_start(self):
        self._fake_solver(True)
        api, _ = self._api()
        how, _d = vyce.unblock_renewal(api, {}, "srv-1", "late", "c=1")
        self.assertEqual(how, "start")

    def test_running_server_goes_to_solver(self):
        self._fake_solver(True, "浏览器内续期返回 200")
        api, holder = self._api()
        how, detail = vyce.unblock_renewal(api, {}, "srv-1", "ok", "c=1")
        self.assertEqual(how, "solver")
        self.assertIn("200", detail)
        self.assertNotIn("last", holder, "运行中的机器不该走 /start")

    def test_start_failure_falls_back_to_solver(self):
        self._fake_solver(True)

        class BadStart(FakeApi):
            def request(self, method, path, body=None, retries=2):
                raise vyce.ApiError(409, {"error": "conflict", "message": "server is running"})

        how, _d = vyce.unblock_renewal(BadStart([[]]), {}, "srv-1", "stopped", "c=1")
        self.assertEqual(how, "solver")

    def test_all_paths_failing_returns_none(self):
        self._fake_solver(False, "找不到 Chrome")
        api, _ = self._api()
        how, detail = vyce.unblock_renewal(api, {}, "srv-1", "ok", "c=1")
        self.assertIsNone(how)
        self.assertIn("Chrome", detail)


class SolverOutputTest(unittest.TestCase):
    def test_parses_token_and_renew(self):
        out = 'TOKEN:bc1.abcdefghijklmnopqrstuvwxyz\nRENEW:{"status":200,"body":"{\\"ok\\":true}"}\n'
        token, renew = vyce.parse_solver_output(out)
        self.assertTrue(token.startswith("bc1."))
        self.assertEqual(renew["status"], 200)

    def test_parses_token_only(self):
        token, renew = vyce.parse_solver_output("TOKEN:bc1.xyz")
        self.assertEqual(token, "bc1.xyz")
        self.assertIsNone(renew)

    def test_ignores_garbage(self):
        token, renew = vyce.parse_solver_output("BESTCAP:retry 1\nRENEW:not-json\n")
        self.assertEqual(token, "")
        self.assertIsNone(renew)


def vyce_capture(api, cfg=None):
    buf = io.StringIO()
    with redirect_stdout(buf):
        failures = vyce.renew_once(api, cfg or {}, force=True)
    return failures, buf.getvalue()


if __name__ == "__main__":
    unittest.main(verbosity=2)
