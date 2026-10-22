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

### 需要真正下单时（M1）

1. 浏览器登录 https://vyce.cloud（GitHub OAuth，账号需 ≥ 30 天）。
2. DevTools → Network → 任一 `/api/` 请求 → 复制请求头里的整行 `Cookie:`，存成项目根目录 `session.txt`（单行）。
3. 验证：`python src/vyce.py auth-check`
4. 自动抢：

```bash
python src/vyce.py watch --deploy          # 检测到名额即下单
python src/vyce.py watch --deploy --dry-run # 演练：只打印将要提交的请求
```

下单成功后会写 `data/last-server.json`，并打印 `ssh -p <port> root@<host>`。

## 命令

| 命令 | 作用 |
|---|---|
| `status` | 打印当前名额、各区域 `freeOpen`、账号/免费机数量 |
| `watch [--interval 2] [--seconds N] [--deploy] [--dry-run]` | 轮询库存；状态翻转写 `data/stock.jsonl`，`--deploy` 时自动下单 |
| `auth-check` | 验证 `session.txt` 的 Cookie 与账号资格（GitHub 龄、SSH key） |
| `deploy [--dry-run]` | 名额已开放时手动下单一次 |
| `stats` | 汇总放号时段分布与最近记录 |

## 配置

复制 `config.example.json` 为 `config.json`：

```json
{
  "poll_interval": 2.0,
  "min_request_interval": 1.0,
  "server_name": "vyce-mini",
  "os": "ubuntu-24",
  "region_preference": ["Europe", "Asia", "USA"],
  "key_ids": []
}
```

- `os`：`ubuntu-24` / `ubuntu-22` / `debian-12` / `fedora-41` / `alma-9`
- `key_ids`：留空 = 用账号里所有已登记公钥
- `server_name`：3–24 位小写字母/数字/连字符（站点正则）

## 设计边界（重要）

- **单账号、单机**：ToS 规定一人一号、一个免费实例；工具下单前会检查是否已有 `source=free` 的机器，有则拒绝。
- **不自动续期**：面板的 Renew 按钮先弹广告再调 `/renew`，脚本代点等于刷广告量。工具只提醒，续期请手动完成。
- **不轰炸**：默认 2s 轮询 ≈ 30 次/分钟，是站点 300 次/分钟限流的 10%；遇 429 按 `x-ratelimit-reset` 退避。
- **不碰账号体系**：不代注册、不绕 30 天账号限制、不多开、不并发多 IP。

## 延迟说明

实测本机到站点单次往返约 600–700ms（Cloudflare LAX）。名额窗口很短时，地理位置决定了胜负；若长期抢不到，M3 可把监听器放到更近的节点，但**不要**用多 IP 并发。

## 目录

```
src/vyce.py           单文件 CLI（Python 3.8+ 标准库）
tests/test_watch_sim.py  离线验证翻转检测/落盘/统计
docs/01-立项.md        立项书：取证、范围、架构、里程碑、风险
data/stock.jsonl      放号翻转记录（自动生成，已 gitignore）
session.txt           会话 Cookie（自动生成，已 gitignore）
```
