# vyce-cloud-tool

vyce.cloud 免费 Vps-Mini 名额的**个人用**监听 / 开抢工具。单账号、单实例、零第三方依赖。

> 立项与可行性取证见 [`docs/01-立项.md`](docs/01-立项.md)。
> 实测结论：放号状态由公开接口 `GET /api/stock` 暴露（`free.open`），站点自己 30 秒轮一次；本工具压到 1–2 秒，只在名额真正放出时下单。

## 快速开始

```bash
# 1. 看一次当前名额（只读，不需要登录）
python src/vyce.py status

# 2. 持续监听（默认 2s 一次；名额放出会响铃 + 写日志）
python src/vyce.py watch

# 3. 统计已记录的放号时段
python src/vyce.py stats
```

### 续期（24 小时冷却窗口）

```bash
python src/vyce.py renew                 # 检查一次，可续就续
python src/vyce.py renew --dry-run       # 只判断状态
python src/vyce.py renew --force         # 无视冷却直接试（服务端可能 429）
python src/vyce.py renew --loop          # 常驻守护，默认每 30 分钟检查
python src/vyce.py watch --deploy --renew # 抢机 + 续期合到一个进程
```

续期状态机与站点前端 `Hy()` 完全一致（`source=free` 且 `status=running` 时按 `expiresAt` 判断）：

| 剩余时间 | 状态 | 能否续期 |
|---|---|---|
| > 24 小时 | `cooldown` | 否，等冷却走完 |
| 24 小时 ≥ 剩余 > 6 小时 | `ok` | 是 |
| ≤ 6 小时 | `soon` | 是 |
| ≤ 0（已过期） | `late` | 是，机器可能已停机 |
| `status != running` | `stopped` | 是，续期会顺带开机 |
| `suspended` / `creating` / 付费机 | `none` | 跳过 |

续期结果写入 `data/renew_state.json`；**每次续期后都会回读 `/api/servers` 确认 `expiresAt` 真的推进**，只信 200 不算成功。服务端用 `429 renewal_cooldown` 表达「冷却未到」，属于预期而非故障，会按 `wait N more hours` 计算下次窗口。

### 需要真正下单时（M1）

1. 浏览器登录 https://vyce.cloud（GitHub OAuth，账号需 ≥ 30 天）。
2. DevTools → Network → 任一 `/api/` 请求 → 复制请求头里的整行 `Cookie:`，存成项目根目录 `session.txt`（单行）。
3. 验证：`python src/vyce.py auth-check`
4. 登记公钥（建机必需）：`python src/vyce.py key-add --pub ~/.ssh/id_ed25519.pub`
5. 自动抢：

```bash
python src/vyce.py watch --deploy          # 检测到名额即下单
python src/vyce.py watch --deploy --dry-run # 演练：只打印将要提交的请求
```

下单成功后会写 `data/last-server.json`，并打印 `ssh -p <port> root@<host>`。

## 命令

| 命令 | 作用 |
|---|---|
| `status [--region R] [--any-region]` | 打印当前名额、各区域 `freeOpen`、账号/免费机数量 |
| `watch [--interval 2] [--seconds N] [--deploy] [--dry-run] [--region R] [--any-region]` | 轮询库存；状态翻转写 `data/stock.jsonl`，`--deploy` 时自动下单 |
| `auth-check` | 验证 `session.txt` 的 Cookie 与账号资格（GitHub 龄、SSH key） |
| `key-add [--pub PATH] [--name N]` | 把本地公钥登记到账号（建机前置） |
| `deploy [--dry-run] [--region R] [--any-region]` | 名额已开放时手动下单一次 |
| `renew [--force] [--dry-run] [--loop] [--interval N]` | 免费机续期；`--loop` 常驻守护（默认 30 分钟一轮） |
| `stats` | 汇总放号时段分布与最近记录 |

## 自定义区域

站点只有三个区域：`Europe` / `Asia` / `USA`（取自 `/api/stock` 的 `regions[].id`）。三种玩法：

```bash
# 只要亚洲：其他区域即使放号也不下单
python src/vyce.py watch --deploy --region asia

# 多个区域，按给定顺序优先（Europe 先，其次 USA）
python src/vyce.py watch --deploy --region eu,us

# 不管配置怎么写，这次哪个区先开抢哪个
python src/vyce.py watch --deploy --any-region
```

- `--region` 可重复、可用逗号、可用全角逗号；认别名 `eu/asia/us/ap/na/欧洲/亚洲/美国`，大小写不敏感。
- 拼错的区域**直接报错退出**（`[x] 未知区域：asia-pacfic；站点当前区域为 ...`），不会静默变成「一直监听却永不抢」。
- 写进配置则用 `region_allow`（严格白名单，`[]` = 不限）：

```json
{ "region_preference": ["Europe", "Asia", "USA"], "region_allow": ["Asia"] }
```

`region_preference` 和 `region_allow` 的分工容易混，记住一句：

| 配置项 | 作用 | 区域外放号时 |
|---|---|---|
| `region_preference` | 只决定**同一批候选里的先后顺序** | 照样会抢 |
| `region_allow` / `--region` | **严格白名单**，范围外的区域直接忽略 | 不下单，日志写「名额开在 X，但不在你指定的区域」 |

`data/stock.jsonl` 里的 `free_regions` 始终记**实际开放的全部区域**，另存一份 `eligible_regions`（过滤后）。这样限定区域的用户也不会把统计样本弄丢。

## 配置

复制 `config.example.json` 为 `config.json`：

```json
{
  "poll_interval": 2.0,
  "min_request_interval": 1.0,
  "server_name": "vyce-mini",
  "os": "ubuntu-24",
  "region_preference": ["Europe", "Asia", "USA"],
  "region_allow": [],
  "key_ids": []
}
```

- `os`：`ubuntu-24` / `ubuntu-22` / `debian-12` / `fedora-41` / `alma-9`
- `key_ids`：留空 = 用账号里所有已登记公钥
- `server_name`：3–24 位小写字母/数字/连字符（站点正则）
- `region_allow`：严格白名单，`[]` = 不限区域

## 设计边界（重要）

- **单账号、单机**：ToS 规定一人一号、一个免费实例；工具下单前会检查是否已有 `source=free` 的机器，有则拒绝。
- **自动续期已实现（M2，按用户要求）**：面板 Renew 按钮是「先弹广告再调接口」，但 `/api/servers/{id}/renew` 本身不校验广告令牌——广告门是纯前端 `sessionStorage` 逻辑。本工具直接调接口，因此**不会产生广告曝光**。这是本工具唯一一处明确绕开站点变现路径的行为，风险自担；服务端若将来加校验，工具会收到 4xx 并如实报错，不会伪装成功。
- **不轰炸**：默认 2s 轮询 ≈ 30 次/分钟，是站点 300 次/分钟限流的 10%；遇 429 按 `x-ratelimit-reset` 退避。
- **不碰账号体系**：不代注册、不绕 30 天账号限制、不多开、不并发多 IP。

## 延迟与连通性（重要）

实测本机到站点单次往返约 500–1200ms（Cloudflare LAX）。名额窗口很短时，地理位置决定了胜负。

**免费机是 IPv6-only**：`connect`（IPv4）为 null，只有 `ipv6`。前端也是 `ssh root@<ipv6>`。
本机 IPv6 出网正常，但到该机房前缀（`2a11:6c7:1105:1906::/64`）所有端口 TCP 超时 —— 这条线路不通。
日常操作请用站点面板里的 **Terminal 标签页**（实测可用，root shell）。

## 已实测结果（2026-10-22）

- 19:54:17 抓到名额（Europe），19:54:22 建机，20:02:23 转 `running`（建机约 8 分钟）。
- 规格：Ubuntu 24.04.5 / x86_64 / **1 vCPU / 2048 MB / 6.5 GB**，load average 15–21（超卖节点）。
- 20:31、20:33 又出现两轮放号，名额不算罕见。

## 目录

```
src/vyce.py           单文件 CLI（Python 3.8+ 标准库）
tests/test_watch_sim.py  离线验证翻转检测/落盘/统计
docs/01-立项.md        立项书：取证、范围、架构、里程碑、风险
data/stock.jsonl      放号翻转记录（自动生成，已 gitignore）
session.txt           会话 Cookie（自动生成，已 gitignore）
```
