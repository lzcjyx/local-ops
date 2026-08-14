# ADR 0002: PlatformAdapter 边界与 Windows 运行时支持

- 状态：Accepted
- 日期：2026-08-15
- Milestone：M2

## 背景

M1 把 OS 无关策略提取进 `adcc/`，但 `server.py` 仍直接执行 `ps`/`lsof`、
`/bin/bash`、`osascript`、`fcntl`、PGID/信号和 `os.getuid`，导入期即依赖
macOS（Windows 上 `import fcntl` 直接失败）。SPEC §7 要求所有 OS 专属进程
逻辑进入 `PlatformAdapter`，并让 Windows 成为一等目标。

## 决策

### 接口与注入

- 新增 `adcc/platform/`：`base.py`（`PlatformAdapter` 抽象基类 +
  `PlatformUnsupportedError`/`PlatformCapabilityError`/`ProcessControlError`
  + `get_platform_adapter()` 工厂 + 共享 `run_cmd`）、`macos.py`、
  `windows.py`、`unsupported.py`（Linux 占位，typed error）。
- Adapter 只做「采集 OS facts」与「执行控制原语」；文本解析仍由
  `adcc.runtime.*` 纯函数完成（与 M1 分层一致）。
- `server.py` 保留原函数名（`scan_listeners`、`ps_snapshot`、`start_app`、
  `acquire_instance_lock`、`pick_path`……）作为 adapter 的薄包装，HTTP、
  payload、monkeypatch Seam 与 macOS 行为不变。
- `server.py` 导入期不再调用 `os.getuid`；`SELF_UID = adapter.current_user_id()`
  （macOS 返回 uid 数值，Windows 返回用户名，只与同源比较）。
- `signal.SIGKILL` 在 Windows 的 `signal` 模块不存在，server 使用
  `SIGKILL = getattr(signal, "SIGKILL", 9)` 常量。

### macOS adapter

原样迁移 ps/lsof/osascript/flock/PGID 语义与 Finder PATH 注入。受管启动
仍由外层 bash 控制器在 argv[0] 持有 `console-run:<token>` 标记并等待后台
作业；停止仍按「先 PGID/UID/token 校验、再 killpg、超时不升级」执行。

### Windows adapter（标准库 + 系统工具，零依赖）

- 进程采集：PowerShell `Get-CimInstance Win32_Process` 单次全量查询
  （~1.5s），TTL 2s 缓存，按 PID 在 Python 侧过滤；owner 通过 SessionId
  判定（交互会话 = 当前用户，Session 0 服务进程 = 非当前用户）。
- 端口采集：`netstat -ano` → 新纯函数 `parse_netstat_listeners`
  （`0.0.0.0` 归一为 `*`，产出与 lsof 解析同构的 `{(pid, port): {host}}`）。
- cwd：Windows 标准库拿不到进程 cwd → `{}`（显式 unknown 降级，SPEC §7）；
  因此 legacy/attached 身份（依赖 cwd 四重校验）在 Windows 不建立，外部
  监听者只报「端口被占用」，绝不被认领或停止。
- cpu：单点快照无法计算百分比 → 0.0（unknown 语义）；mem 用
  WorkingSetSize 占物理内存百分比近似；etime 由 CreationDate 计算。
- 受管身份：Windows 无 POSIX 进程组、无法读他人环境变量。启动通过
  `cmd.exe /d /s /c "set "CONSOLE_RUN_TOKEN=<token>" && <command>"` 包装，
  令牌出现在可查询的命令行；身份 = lastPid 存活 + 当前用户 + 命令行携带
  token 标记，随后代树（CIM ParentProcessId BFS）视为受管
  （新增纯函数 `managed_process_index_windows`）。
- 停止语义：`taskkill /PID [/T]` 无 `/F` 先尝试优雅（GUI 程序 WM_CLOSE）；
  对控制台程序 taskkill 会返回「只有强制终止才能终止」，此时自动升级
  `/F` 重试——Windows 控制台进程没有 SIGTERM，硬终止是唯一停止机制。
  目标已经过受管身份校验，升级不会误杀外部进程。`kill_process` 的
  force 标志仍严格区分，不隐式升级。
- **关键发现**：Windows 上 `os.kill(pid, 0)` 语义是 TerminateProcess
  （会杀死探测目标），因此 `pid_alive` 在 Windows 用 ctypes
  `OpenProcess` + `GetExitCodeProcess(STILL_ACTIVE)` 实现，绝不调用
  `os.kill(pid, 0)`。
- 单实例锁：`msvcrt.locking`（字节范围锁，进程崩溃自动释放）。
- 目录选择：PowerShell WinForms 对话框（STA）；launcher 对话框在 Windows
  降级为 None（仅 macOS 使用）。
- 数据目录：Windows 默认 `%APPDATA%\总控台` 与 `%APPDATA%\总控台\logs`。

### 测试平台化

- 新增 `tests/test_platform.py`：netstat 解析纯函数、CIM payload 解析、
  unsupported typed error、Windows 真实运行冒烟（listeners/快照/身份/
  树/生命周期启停/外部进程安全）。
- 既有 macOS 语义守卫（ps/lsof 文本、PGID/信号、bash 脚本、X_OK、
  uid 数值、symlink）在 Windows 上 `skipUnless(darwin)`；跨平台断言
  （python vs python3 候选命令、文件权限位、日志尾随换行）改为平台分支。
- `check_project.py` 语法检查已递归覆盖 `adcc/`（M1 已含）。

## 结果

Windows 本地：`python server.py` 可导入启动；服务监控枚举正常；受管服务
「启动 → 端口发现 → 身份识别 → 停止」全链路通过；外部进程占用配置端口
时不被认领、不被停止；145 项测试通过、40 项 macOS 专属跳过。
macOS 行为由既有测试守卫（类级 `skipUnless(darwin)` 在 macOS CI 照跑）。

代价与限制：

- Windows 上 cwd 不可得 → 认领（attach）与升级前 legacy 身份不可用；
- Windows 停止对控制台进程是硬终止（无 SIGTERM 平台现实，已文档化）；
- Windows 首次 state 构建 ~1.7s（一次 CIM 全量），缓存命中后 ~10ms；
- `schedule_console_restart` 的 helper 在 Windows 改为拉起新进程而非
  `os.execv`（Windows 无 execv）。

## 未采用方案

- 用 `wmic`：已弃用且 Win11 24H2 移除。
- 用 pywin32/psutil：违反「标准库零依赖」约束。
- Windows 上为每个 PID 调 `GetOwner()`：全量时每进程 ~10ms，无法满足
  轮询预算；SessionId 判定在本地单用户开发场景等价且快。
- 停止时对已校验目标直接 `taskkill /F`：丢失 GUI 程序优雅退出机会。
