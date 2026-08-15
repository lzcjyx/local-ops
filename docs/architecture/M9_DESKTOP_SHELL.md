# M9 Tauri 2 桌面壳

## 结论

`desktop/src-tauri`（Rust）是 daemon 的轻量壳：发现/启动 daemon，
webview 承载既有 web UI，托盘常驻，关窗不退出。Core 逻辑零重复。
详见 `docs/adr/0009`。

```text
总控台.app / .exe (Tauri 2 shell)
   │ 读 daemon.json（数据目录）
   ├─ 有健康 daemon ──────────┐
   └─ 无 ── spawn python server.py --no-browser
                              ▼
                    http://127.0.0.1:<port>/   (webview)
                              │
                          daemon (独立进程，壳退出不影响)
```

## 壳能力

| 能力 | 实现 |
| --- | --- |
| daemon 发现 | daemon.json + std TcpStream 健康探测 |
| daemon 启动 | spawn python[3] server.py --no-browser，15s 轮询 |
| webview | 导航到 daemon HTTP UI（前端零改动） |
| 托盘 | 打开控制台/数据目录/重启 daemon/退出；左键单击显示 |
| 关窗 | prevent_close + hide（daemon 与受管服务继续） |
| 通知 | 启动/连接/失败（tauri-plugin-notification） |
| token handoff | 首次访问自动签发 control cookie（既有机制） |

## 构建与打包

- `cargo tauri build` → NSIS 安装包（WiX MSI 中文名本地化问题已规避）
- 资源随包：server.py / adcc / static / VERSION / LICENSE
- CI：Windows + macOS 矩阵 `cargo build --release` 编译 smoke

## Exit gate 验证

- 桌面产物启动 ✓（debug/release 均验证）
- daemon 可达 ✓（daemon.json + /api/health 200）
- 托盘 ✓（编译 + 人工验收；GUI 交互无法自动化）
- 加项目 ✓（UI 经 daemon 的既有功能）
- 服务启停 + 日志 ✓（daemon 侧既有能力，webview 复用）
- 关窗不丢状态 ✓（壳退出后 daemon 独立存活，实测）

## 已知限制（发布前）

- 未签名/未公证（M11 处理）
- 无自动更新/开机自启（P1）
