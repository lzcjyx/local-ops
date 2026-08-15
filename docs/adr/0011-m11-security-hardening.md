# ADR 0011: M11 安全加固——本地控制凭证

- 状态：Accepted
- 日期：2026-08-15
- Milestone：M11

## 背景

M11 审计发现：mutating 请求对**无头本地客户端**（CLI/MCP，无
Origin/Sec-Fetch-Site 头）不要求任何凭证——只有浏览器路径经
cookie 校验。任何本地进程都能 POST 写接口，未达到 SPEC §16.1
「mutating requests must enforce local authentication」。

## 决策

### 强制本地凭证

- `authorize_request(mutating=True)` 现在要求**任一**凭证：
  - 浏览器：HttpOnly `console_session` cookie（原有，SameSite=Strict）；
  - 无头客户端：`X-ADCC-Token` 头 == `daemon.json` 中的随机令牌。
- 两者都走 `secrets.compare_digest`（常量时间）。
- 跨站（site/origin）分支维持原有 cookie 强校验（token 不豁免跨站）。
- 读操作（GET）保持回环信任边界（无凭证）。

### 令牌分发

- daemon 每次启动生成 `control_token`（M4 起已有）；`daemon.json`
  （0600）持久化 port/pid/token。
- CLI `DaemonClient` 与 MCP（复用同一 client）自动携带
  `X-ADCC-Token`；桌面壳 webview 走 cookie（首访签发）。
- 令牌随 daemon 重启轮换；不写入日志。

### 其他加固（本里程碑）

- 模板注入测试：agent adapter 的 argv/env 渲染是**数组/原值**（无
  shell 解释），占位符值含 `;`/`$()` 等元字符时原样传递——测试
  固化该语义。
- SQLite 损坏：`RunDatabase.__init__` 迁移失败时关闭连接再抛出，
  server 的 `get_runs_db` 降级 None（API 空、不阻塞运行）——测试
  固化。
- macOS `.app`/`.dmg` 打包加入 CI（`cargo tauri build`），Windows
  NSIS 本机已验证（WiX 中文名问题记录于 ADR-0009）。
- SECURITY.md 更新本地凭证模型。

## 结果

- 新增测试：无凭证/错误 token → 403；正确 token → 200；模板注入
  原样传递；损坏 DB 降级。251 项测试全绿；双平台 CI + macOS 打包
  验证通过。

代价与限制：

- 未签名/未公证的桌面产物（发布前限制，记录于已知限制）；
- 浏览器 cookie 与 token 双通道并存（迁移期兼容，未来可只留其一）。

## 未采用方案

- 仅 token、废除 cookie：webview/浏览器首次访问需额外握手，无收益。
- 无凭证放行 + 仅文档：不满足 SPEC §16.1 的强制执行要求。
