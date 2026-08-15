# ADR 0009: Tauri 2 桌面壳

- 状态：Accepted
- 日期：2026-08-15
- Milestone：M9

## 背景

SPEC §19 要求桌面产品（Tauri 2 shell），且 Core 逻辑不得搬入 Rust。
P0 复用既有 web UI——桌面壳只需启动/连接 daemon 并用 webview 承载
daemon 的 HTTP UI。

## 决策

### 架构：壳即客户端

- `desktop/src-tauri`（Rust）：**不实现任何进程/端口/编排逻辑**。
  webview 加载 `http://127.0.0.1:<port>/`（daemon 的既有 UI，
  前端零改动）。
- daemon 发现：读 `DATA_DIR/daemon.json`（与 CLI 同规则：Windows
  `%APPDATA%\总控台`，macOS `~/Library/Application Support/总控台`，
  `CONSOLE_DATA_DIR` 可覆盖）→ `http_health` 探测（std TcpStream
  手写最小 HTTP GET，避免 reqwest）。
- daemon 启动：无健康 daemon 时 spawn `python[3] server.py
  --no-browser`（打包后从资源目录运行；开发时从仓库根），轮询
  daemon.json 健康（15s 超时）。**daemon 是独立进程**——桌面壳退出
  不影响 daemon 与受管服务（已验证）。
- token handoff：webview 首次访问 `/api/state` 由既有 loopback
  授权流程签发 HttpOnly cookie（control_token），后续写操作自动
  携带——无需额外机制（M11 再收紧）。

### 壳行为

- 托盘（tauri 2 tray-icon）：打开控制台 / 打开数据目录 / 重启 daemon
  / 退出；左键单击显示窗口。
- 窗口关闭 → `prevent_close` + hide（daemon 继续运行）；托盘退出才
  真正结束壳。
- 通知（tauri-plugin-notification）：daemon 启动/连接/失败提示。
- 图标：`tauri icon` 从既有 `console-app-icon.png` 生成全套。

### 打包

- `tauri.conf.json`：productName 总控台、identifier `cn.adcc.console`、
  bundle.resources 含 `server.py/adcc/static/VERSION/LICENSE`，
  Windows 目标 **NSIS**（WiX MSI 在中文 productName 下 light.exe
  本地化变量报错，NSIS 正常）。
- CI：Windows/macOS 矩阵 `cargo build --release` 编译 smoke。

## 结果

- 本地验证：debug/release 壳启动 → daemon 自动拉起（daemon.json +
  /api/health 200）；关闭壳后 daemon 独立存活；NSIS 安装包产出
  （`总控台_1.0.0_x64-setup.exe`）。
- exit gate 六项在本机全部满足（启动产物、daemon 可达、托盘、加项目
  （UI 经 daemon）、服务启停+日志、关窗不丢状态）；托盘/通知为
  GUI 行为，机器上无法自动化点击，以编译 smoke + 人工验收记录。

代价与限制：

- 桌面产物未签名/未公证（M11 记录为发布前限制）；
- 壳未实现自动更新与"开机自启"（P1）；
- `restart_daemon` 仅重启 daemon 进程（受管服务不动）。

## 未采用方案

- webview 内嵌前端构建产物：daemon 已服务静态资源，重复打包无收益。
- Rust 侧复制运行时逻辑：违反 SPEC §19.1。
- MSI 目标：WiX 本地化问题；NSIS 满足打包需求。
