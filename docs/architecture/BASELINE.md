# ADCC M0 基线记录

## 目的与范围

本文记录 ADCC 迁移开始前的 local-ops 上游基线、可用工具链和测试证据。M0 只做审计与文档化，不改变运行时行为，也不提前实现 M1 的模块拆分。

## 上游基线

| 项目 | 值 |
| --- | --- |
| SPEC 指定上游 | `laogou717/local-ops` |
| SPEC 指定分支 | `main` |
| 基线提交 | `a5c3adae1f1fa0bd9f0ac7b090ec422e285d0c0f` |
| 提交主题 | `feat: 顶栏新增 GitHub 仓库链接按钮` |
| 提交时间 | `2026-08-11T19:30:13+08:00` |
| 本地审计分支 | `feat/adcc` |
| 本地 `origin` | `https://github.com/lzcjyx/local-ops.git` |
| `VERSION` | `1.0.0` |

审计开始时 `HEAD` 与 SPEC 指定的基线提交完全一致，`origin/main` 和本地 `main` 也指向该提交。本地 `origin` 是该代码库的另一个远端地址，不改变提交级基线。审计前没有已跟踪文件差异；`ADCC_SPEC.md`、`ADCC_PLAN.md`、`CODING_AGENT_PROMPT.md` 是尚未跟踪的 ADCC 输入文档。

## 当前产品边界

- 运行时后端是 4,071 行的单文件 `server.py`，仅使用 Python 标准库，但直接依赖 POSIX/macOS API 和系统命令。
- 前端是本地静态资源与原生 ES Modules，无构建、无 CDN、无框架。
- 当前正式支持平台是 macOS 12+，要求 Python 3.12、`ps`、`lsof`、`osascript`、Bash 和 `plutil`。
- 当前 CI 只有 `macos-15`，使用 Python 3.12，执行项目检查和两次可复现发行构建验证。
- 默认只绑定 `127.0.0.1`，端口范围为 9600–9609。
- 用户配置与日志默认位于 macOS Library 目录，项目内 `data/` 仅作为旧数据迁移源。

详细职责和依赖见 [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md)，HTTP 契约见 [API_BASELINE.md](API_BASELINE.md)。

## 权威检查入口

仓库明确指定的提交前检查是：

```text
make check
```

`Makefile` 的 `check` target 最终调用：

```text
$(PYTHON) tools/check_project.py
```

该入口依次执行必要文件、版本、Python/JavaScript 语法、ES Module 绑定、Bash/plist、依赖锁定、素材来源、主题、静态引用、生成图标同步、Python unittest 和 Node 行为测试。

单独测试命令为：

```text
python -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/js/ports.test.mjs
```

`make test` 只运行 Python unittest，不等价于完整检查。`make release-check` 是发行门禁，不是日常 M0 检查的替代品。

## 2026-08-14 可用平台执行证据

### 审计主机

| 工具 | 版本/状态 |
| --- | --- |
| OS | Windows 10 Pro 64-bit, `10.0.19045` |
| PowerShell | `7.6.4` |
| Python | `3.12.4` |
| Node.js | `24.14.0` |
| Git | `2.52.0.windows.1` |
| `make` | 不可用 |
| `/bin/bash`、`plutil`、`lsof`、`osascript` | 原生 Windows 不可用 |

由于本机没有 `make`，M0 在 Windows 上直接运行了该 target 的实际检查器 `python tools/check_project.py`；这里的 `python` 解析为 `E:\anaconda3\python.exe` 3.12.4。

### 精确基线提交的 macOS CI 证据

上游仓库对精确提交 `a5c3adae1f1fa0bd9f0ac7b090ec422e285d0c0f` 有一份成功的 [GitHub Actions CI 运行](https://github.com/laogou717/local-ops/actions/runs/31486920212)：

| 项目 | 证据 |
| --- | --- |
| Workflow / job | `CI` / `Check and release audit` |
| Runner | macOS 15.7.7，`macos-15-arm64` |
| Python / Node | CPython 3.12.10 / Node 22.23.1 |
| 命令 | `make check PYTHON=python` |
| 结果 | 13 项检查通过，0 项人工提醒 |
| Python tests | 159 tests，`OK` |
| JavaScript tests | 7 tests，通过 |
| 发行验证 | 两次 build + verify + `cmp` 通过，SHA-256 一致 |

该运行由上游 `laogou717/local-ops` checkout 精确 SHA 后执行，时间为 2026-08-11。它为当前正式支持的 macOS 基线提供可复查的完整检查证据；M0 本机 Windows 运行用于补充记录当前跨平台阻塞，而不是替代这份 macOS 证据。

### 完整项目检查结果

`python tools/check_project.py`：失败，10 项通过、3 项失败。

通过项：

- 26 个必要文件；
- 版本一致性；
- 10 个 Python 文件语法；
- 8 个 JavaScript 文件语法；
- 7 个模块/41 个公共导出绑定；
- 开发依赖锁定；
- 7 个素材文件来源登记；
- `ops` 主题注册；
- 静态资源与本地模块引用；
- 56 个生成图标同步。

记录的基线失败：

1. `tools/check_project.py` 固定调用 `/bin/bash -n` 和 `plutil -lint`，原生 Windows 无这些 macOS/POSIX 工具。
2. Python 测试在导入 `server.py` 时遇到 `ModuleNotFoundError: fcntl`，因此 `test_server.py` 与 `test_hardening.py` 无法加载。
3. Node 7 项测试实际通过，但检查器只识别旧 TAP 文本 `# pass N`；Node 24 输出 `ℹ pass 7`，检查器报“无法确认 node --test 结果”。

### Python unittest 结果

直接运行 unittest：发现 36 个结果，32 通过、1 失败、3 错误。

| 结果 | 证据 | 基线原因 |
| --- | --- | --- |
| ERROR | `test_hardening` 模块导入失败 | `server.py` 无条件导入 POSIX `fcntl` |
| ERROR | `test_server` 模块导入失败 | 同上 |
| ERROR | `test_symlinked_required_source_is_rejected` | Windows 未启用创建符号链接所需权限，`WinError 1314` |
| FAIL | `test_archive_is_reproducible_and_metadata_is_normalized` | Windows 文件模式返回 `0666`，测试断言 POSIX `0644` |

测试源码静态盘点共有 159 个 Python `test_*` 方法。由于两个大模块在 discovery 阶段无法导入，Windows 本次没有执行其中 125 个测试。这是 M2 必须通过平台抽象与 Windows 测试覆盖解决的既有边界，不在 M0 修改。

### JavaScript 行为测试结果

直接运行 `node --test tests/js/ports.test.mjs`：7/7 通过，无失败、取消或跳过。

### macOS 验证边界

当前工作环境不是 macOS，无法在本机执行 `/bin/bash`、`plutil`、`lsof`、`osascript`、真实 PGID/信号流程或 `.app` 启动器交互验收。M0 使用上述精确提交的 macOS 15 CI 作为自动检查证据，但不把它表述为本次会话的本地桌面人工验收。M0 同时在可用的 Windows 主机运行了检查入口，并把所有失败与未覆盖范围明确记录。

## 测试资产数量

| 测试文件 | 静态数量 | 主要保障 |
| --- | ---: | --- |
| `tests/test_server.py` | 83 | 解析、项目检测、配置/迁移、进程身份、状态、日志、favicon、诊断、主题 |
| `tests/test_hardening.py` | 42 | HTTP 安全、原子认领、操作锁、生命周期、路径穿越、kill、日志边界、缓存 |
| `tests/test_frontend.py` | 14 | ARIA、对比度、窄屏、认领流程、任务状态、端口发现、键盘排序、品牌 |
| `tests/test_release.py` | 16 | 发行边界、敏感信息、符号链接、可复现包、许可与素材门禁 |
| `tests/test_project_checks.py` | 4 | ES Module 公共调用绑定检查 |
| `tests/js/ports.test.mjs` | 7 | 端口归一化、错位、显示和打开规则 |
| 合计 | 166 | 159 Python + 7 Node |

仓库没有平台 `skip`/`skipIf`；当前测试套件把 macOS/POSIX 假设直接编码在导入和行为测试中。

## 必须保持的兼容与安全不变量

1. 不以端口单独认定或停止受控进程。
2. 新进程身份必须结合随机 run token、PGID 和当前 UID。
3. legacy/attached 认领必须满足配置端口、当前 UID 和真实 cwd，并且匹配唯一。
4. 停止仅作用于已验证身份；普通停止不自动升级为 `SIGKILL`。
5. 外部进程占用配置端口时只报告冲突，不杀、不自动认领。
6. 配置写入保持锁内更新、备份、`fsync`、临时文件和原子替换；未来 schema 不被旧程序覆盖。
7. 旧 `data/` 迁移只复制、不删除、不覆盖已存在目标，显式目录覆盖时不自动迁移。
8. HTTP 管理面只接受精确 loopback Host/客户端；浏览器写入保持同源和 HttpOnly 会话校验。
9. 静态文件、图标、favicon 和日志读取保持路径/大小/loopback 边界。
10. 前端继续按 key 原地对账，并以 `/api/state` 为唯一状态轮询入口。

## M0 Exit Gate

| 条件 | 结论 | 证据 |
| --- | --- | --- |
| 当前支持测试通过，或每个既有失败有证据 | 满足 | 精确 SHA 的 macOS CI：159 Python + 7 Node 全通过；Windows 基线失败逐项记录 |
| 无有意行为变化 | 满足 | M0 只新增架构文档并更新计划状态；运行时代码、前端和测试未修改 |
| 主要 `server.py` 职责与 OS 依赖已记录 | 满足 | `CURRENT_ARCHITECTURE.md` 与 `API_BASELINE.md` |

结论：M0 Exit Gate 已满足。Windows 失败是 M2 需要解决的已知平台边界；本次任务在 M0 边界停止，不进入 M1。
