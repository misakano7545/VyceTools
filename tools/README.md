# tools/

## bestcap_solver.mjs

**来源**：https://github.com/dengyie/vycebump 的 `captcha_solver.mjs`，**原样引入，未修改**。
**许可**：MIT，Copyright (c) 2026 dengyie，见同目录 `LICENSE.vycebump`。

### 它做什么

vyce.cloud 的 `POST /api/servers/{id}/renew` 现在要求请求体带 `captchaResponse`。Bestcap 是随机化的
WASM 虚拟机工作量证明，无法在 Python 里稳定复现，所以这里用「官方组件自己算」：

1. 起一个带 `--remote-debugging-port` 的无头 Chrome，注入会话 Cookie；
2. 打开 `https://vyce.cloud`，用页面自己加载的 `window.bestcap.render()` 渲染控件；
3. 通过 CDP 对验证码 iframe（跨域 OOPIF）里的 `#check` 派发**真实鼠标事件**，触发「我不是机器人」；
4. 从 `Network.loadingFinished` 的 verify 响应体里读出 `{status:"ok", token}`；
5. 传入 server id 时，在同一浏览器会话里 `fetch` 提交续期 —— 来源、UA、TLS 指纹、出口 IP 都与官方前端一致。

**这不是绕过验证码**，而是让官方控件在真实浏览器里跑完它自己的工作。伪造 token 会被服务端拒绝。

### 依赖

- Node 18+（脚本用内置 `fetch` / `WebSocket`，无需 `npm install`）
- Chrome / Chromium / Edge（chromium 内核，支持 `--remote-debugging-port`）

### 单独验证求解器（不提交续期，不碰账号状态）

```bash
# 先手动起一个带调试端口的 Chrome
chrome --headless=new --remote-debugging-port=9222 --user-data-dir=/tmp/x about:blank
# 只求解 token，不续期
node tools/bestcap_solver.mjs http://127.0.0.1:9222
```

预期输出 `TOKEN:bc1.…`。本机实测：19.0 秒、724 字节 token，一次成功。
