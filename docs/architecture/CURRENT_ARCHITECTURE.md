# 当前架构图（M1）

## 总览

当前 local-ops 仍是一个 macOS-first、本地回环控制台。它有单一控制台/HTTP 后端进程，并启动或认领独立的服务/任务进程组。M1 已把配置持久化、文本归一化、受管身份和 task 状态策略提取到 OS-neutral `adcc/` Module；`server.py` 继续承担兼容入口、macOS facts 采集、运行时 orchestration、HTTP API 和启动器协调。浏览器中的原生 ES Modules 只通过本地 HTTP API 读写状态。

M0 的原始基线和精确提交证据见 [BASELINE.md](BASELINE.md)，M1 的 Interface/Implementation 边界见 [M1_CORE_BOUNDARIES.md](M1_CORE_BOUNDARIES.md)。

```text
总控台.app / start.command / python3 server.py
                     |
                     v
              server.py 兼容入口
  +------------------+------------------+
  |                  |                  |
  v                  v                  v
adcc Core        ps/lsof/信号/PGID    HTTP + 静态资源
  |                  |                  |
  v                  v                  v
配置/纯策略       受控服务/任务      原生 ES Module UI
  |                                        |
  v                                        +-- 每 2 秒 GET /api/state
Library JSON                             +-- 多数写操作后 window.__poll
```

这仍不是目标架构。M2 以后会继续通过 PlatformAdapter、Application、CLI/MCP/Tauri 等边界演进；`server.py` 必须继续作为兼容入口，不能大爆炸重写。

## 仓库结构与当前责任

| 路径 | 当前责任 |
| --- | --- |
| `server.py` | 兼容入口、macOS facts/I/O、进程 orchestration、HTTP、启动/重启逻辑 |
| `adcc/core/` | 当前常量、错误和 legacy 结构类型；不含 OS/HTTP |
| `adcc/storage/config.py` | schema 迁移、配置恢复/健康、备份与原子持久化 |
| `adcc/runtime/ports.py` | 端口校验、listener 文本归一化与 open-host 策略 |
| `adcc/runtime/processes.py` | 进程文本解析、分组、项目名和来源归因纯策略 |
| `adcc/runtime/lifecycle.py` | token/PGID/UID 与 legacy/attached 受管身份纯策略 |
| `adcc/runtime/tasks.py` | task 退出码和 legacy last-exit 输出策略 |
| `static/index.html` | 单页结构、所有模块依赖的 DOM ID、加载顺序 |
| `static/app.js` | 前端唯一编排入口、2 秒轮询、视图切换、全局初始化 |
| `static/js/core.js` | DOM/API 工具、共享状态、mutation epoch、reconcile、主题/通知/浮层基础 |
| `static/js/launchpad.js` | 服务/任务卡、KPI、筛选、拖拽/键盘排序、端口诊断、启动/停止/重启 |
| `static/js/services.js` | 监听服务表、会话内新端口发现、关注进程、标记、火花线 |
| `static/js/overlays.js` | 确认框、应用 CRUD、项目检测/选择器、图标、日志抽屉 |
| `static/js/ports.js` | 无 DOM 的端口归一化与展示纯函数 |
| `static/js/widgets.js` | 右侧动态、Top 5、日志/设置中心、批量停止 |
| `static/base.css` | 布局 v2 和基础结构样式 |
| `static/themes/ops.css` / `.json` | 唯一 Ops 视觉皮肤与清单 |
| `static/icons/*.svg` | vendored Lucide 源图标 |
| `static/icons.js` | 由 `tools/gen_icons.py` 生成的受信任图标全局，禁止手改 |
| `start.command` | 有 Terminal 的 Bash 启动入口 |
| `总控台.app` | `LSUIElement` Finder 启动器、Info.plist、AppIcon |
| `tools/check_project.py` | 权威项目检查编排器 |
| `tools/build_release.py` | 发行清单、敏感文件检查、可复现 zip 与校验 |
| `tests/` | Python 行为/安全/发行/前端契约测试与 Node 端口纯函数测试 |

`static/index.html` 先加载 `icons.js`，再加载模块入口 `app.js`。各模块在顶层查询 DOM，因此 DOM ID 和脚本加载顺序是隐含兼容契约。

## `server.py` 职责地图

| 行 | 责任 | 关键入口 |
| --- | --- | --- |
| 84–181 | 运行目录、版本、限制值与路由正则；纯常量/任务策略从 Core re-export | `resolve_runtime_dir`, `read_project_version` |
| 182–340 | 私有目录、旧数据复制迁移、文件权限 | `migrate_legacy_runtime_data`, `prepare_runtime_storage` |
| 341–396 | Core Config 兼容 wrapper 与 POSIX 单实例锁 | `Config`, `acquire_instance_lock` |
| 397–465 | macOS 命令采集 wrapper；解析委托 `adcc.runtime` | `scan_listeners`, `ps_snapshot`, `lsof_cwds` |
| 466–806 | 状态采集、受管身份 wrapper、应用与总状态 | `build_services`, `managed_process_index`, `legacy_managed_pid`, `build_state` |
| 807–909 | 状态 TTL 缓存、轻量健康、主题清单 | `get_state_snapshot`, `build_health`, `list_themes` |
| 910–1342 | 当前用户 kill、进程组停止、PATH、启动/退出监视、配置健康 | `kill_process`, `start_app`, `watch_app_exit`, `inspect_app_health` |
| 1343–1837 | 有界项目识别、受控停止、超时、外部监听进程原子认领 | `detect_project`, `resolve_app_stop_target`, `attach_app_process` |
| 1838–2192 | 日志、favicon、诊断、字段验证与 per-app 操作锁 | `rotate_log_file`, `fetch_favicon`, `diagnose_app` |
| 2193–3286 | Threading HTTP server、请求安全、API、静态/图标服务、CRUD | `ConsoleServer`, `Handler` |
| 3287–3554 | 浏览器、同项目实例识别、macOS 对话框、restart helper、主入口 | `launcher_main`, `restart_helper`, `main` |

## 运行时状态流

### 监听与状态快照

1. `scan_listeners()` 运行 `lsof -iTCP -sTCP:LISTEN -P -n`，以 `(pid, port)` 去重。
2. `ps_snapshot()` 运行 macOS/BSD 风格 `ps` 获取 UID、etime、CPU、内存、comm 和 args。
3. `lsof_cwds()` 获取监听 PID 的真实 cwd。
4. `pgid_members_map()` 找到启动脚本退出后仍在同一 PGID 的后台子进程。
5. `managed_process_index()` 以 PGID + 当前 UID + argv run token 识别新版受控进程。
6. `legacy_managed_pid()` 仅在严格 PID/端口/UID/真实 cwd 条件下兼容旧进程；`attached` 允许 PID 轮换，但仍要求端口、UID、cwd 唯一命中。
7. `build_state()` 合并 `services`、`watched`、`apps`、主题、版本、配置健康和降级信息。
8. 快照以约 2.2 秒 TTL 缓存；配置修改会使缓存失效。

`GET /api/health` 不运行 `ps`/`lsof`，用于轻量健康检查。`GET /api/state` 是完整且相对昂贵的状态入口。

### 启动

1. 启动前只读检查 cwd、脚本、运行时和配置端口当前占用。
2. 每次启动生成随机 `runToken`，放入环境和外层 Bash argv 标记。
3. `/bin/bash` 外层控制器使用 `start_new_session=True` 创建独立进程组，并等待内部命令及其后台作业。
4. 配置原子保存 `lastPid`、`lastPgid`、`runToken`。
5. 服务/任务退出监视线程按 token 防止旧线程覆盖新一次运行身份。
6. task 的自然退出映射为 `succeeded`/`canceled`/`failed`；控制台主动中止记录 `stopped`。

### 停止与认领

- 停止前重新验证 token/PGID/UID；legacy 仅作用于严格身份匹配的 PID。
- 对受控进程组只发 `SIGTERM`，有界等待；超时保留身份供用户重试，不自动升级 `SIGKILL`。
- `/api/kill` 是服务监控中的显式 PID 操作，仍先验证当前用户，并拒绝控制台自身。
- 端口占用只用于诊断或阻止启动，绝不作为停止依据。
- 外部服务认领要求 service、有配置端口、PID 正在监听、当前 UID、真实 cwd；创建来源卡片时在同一配置写锁内复验并原子保存。

## 配置、数据与迁移

### 路径

| 数据 | 默认路径 |
| --- | --- |
| 配置 | `~/Library/Application Support/总控台/config.json` |
| 配置备份 | `~/Library/Application Support/总控台/config.json.bak` |
| 单实例锁 | `~/Library/Application Support/总控台/console.lock` |
| 图标 | `~/Library/Application Support/总控台/icons/` |
| 日志 | `~/Library/Logs/总控台/` |
| 旧数据源 | `<repo>/data/` |

`CONSOLE_DATA_DIR` 和 `CONSOLE_LOG_DIR` 可覆盖默认路径，但必须是非空绝对专用子目录，不能等于文件系统根、用户 home 或项目根。

### 当前 schema

当前 `schemaVersion` 为 1，顶层字段为：

```text
schemaVersion
apps[]
hidden[]
pinned[]
promoted[]
watchedKeywords[]
uiTheme
```

应用当前实际字段包括 `id/name/command/cwd/port/emoji/glyph/icon/favicon/kind/lastPid/lastPgid/runToken/attached/lastExit/createdAt`。

### 迁移与持久化

- 仅在默认目标完全不存在且未设置对应环境变量覆盖时，从旧 `data/` 复制。
- 只复制普通文件，不跟随符号链接；旧目录从不删除或覆盖。
- 目录权限尝试收紧为 0700，配置、锁、图标和日志为 0600。
- schema 迁移是显式链；当前仅有 v0（无版本）到 v1。
- 未来 schema 触发拒绝降级，绝不使用旧备份覆盖较新主配置。
- 主配置不可读时尝试 `.bak`；两者均不可用时以内存空状态启动只读保护，避免覆盖可人工恢复的数据。
- 修改在 `RLock` 内完成：深拷贝旧值，先原子写 `.bak`，再通过 `.tmp` + `fsync` + `os.replace` 写主文件；失败恢复内存旧值。
- 日志超过 10 MiB 时 copy-truncate，保留 3 份；API 只从文件尾有界读取。

## HTTP 与本地安全边界

- server 只绑定 `127.0.0.1`。
- Host 必须是当前控制台端口上的 `127.0.0.1`、`localhost` 或 `::1`，客户端地址也必须是 loopback。
- 浏览器写请求在带 `Origin`/`Sec-Fetch-Site` 时要求精确同源和本进程发放的 HttpOnly、SameSite=Strict 会话 cookie；headerless 本地 JSON 客户端保持兼容。
- headerless 本地 JSON 客户端保持兼容，但写入口仍要求正确 Content-Type、唯一 Content-Length 和大小上限。
- 不支持 Transfer-Encoding，不提供 CORS，OPTIONS 明确拒绝。
- 响应设置 CSP、frame deny、no-referrer、nosniff、same-origin resource/opener 策略。
- 静态文件以 `realpath/commonpath` 防目录穿越和符号链接逃逸。
- favicon 仅允许访问指定 loopback 端口，重定向也受同一限制。
- 不读取 body 的 POST 必须调用 `discard_body()`，否则 keep-alive 上的残留 `{}` 会污染下一请求。

## 前端状态与 DOM 架构

### 主轮询

- `static/app.js` 每 2 秒请求 `GET /api/state`，同一时间只允许一个状态请求。
- 页面隐藏时停止/中止轮询；重新可见时强制刷新。断线恢复和控制台重启依赖既有定时轮询恢复，并静默重建发现基线。
- 写操作通过 `core.js` 增加 mutation epoch；在途旧快照会被丢弃，随后立即补轮，避免成功写入被旧 GET 覆盖。
- `window.__poll` 是跨模块共享刷新入口，属于现有隐含契约。

### 原地对账

`core.js:reconcile()` 保持现有 DOM 节点与事件监听：

- 应用卡 key：`app.id`；
- 服务/发现行 key：`instanceKey || key`；
- 关注进程 key：`pid`。

服务的 DOM 身份使用 `instanceKey`，持久化 hidden/pinned/promoted 仍提交兼容 `key=name:port`。这两个概念不能合并。

### 端口语义

- `app.port` 是配置与启动前检查端口。
- `app.ports[]` 是受控进程组实际监听端口。
- 运行中的打开/复制链接优先真实端口；纯函数集中在 `ports.js` 并由 Node 测试保护。
- 会话内新端口发现和右栏动态是前端差分状态，首次加载、不连续轮询和恢复时只建基线，不持久化。

## macOS/POSIX 依赖清单

| 类别 | 依赖 | 当前用途 | 缺失时表现 |
| --- | --- | --- | --- |
| Python API | `fcntl.flock` | 同一 data dir 单实例 | Windows 在导入期直接失败 |
| Python API | `os.getuid`, `getpgid`, `killpg`, `fchmod`, `execv` | 所有权、PGID、权限、重启 | Windows 无等价直接实现 |
| 进程启动 | `start_new_session` | 独立进程组 | 语义需平台适配 |
| 命令 | `lsof` | TCP listener 与 cwd | `run_cmd` 返回空，状态可能空白 |
| 命令 | BSD/macOS `ps` | UID、comm、args、PPID/PGID、资源 | 解析绑定 macOS 输出 |
| 命令 | `osascript` | 文件/目录选择器、启动器对话框 | 选择器返回失败；对话框静默缺失 |
| shell | `/bin/bash`, `/bin/zsh` | 保存命令、脚本、run-token 控制器 | 应用无法启动 |
| 路径 | `/opt/homebrew`, `/usr/local`, `~/Library`, NVM/fnm/Volta/pnpm | Finder 启动环境补全 | 非 macOS 路径无意义 |
| 进程元数据 | `.app/Contents`, `/Library/Containers`, `launchd` | 后台分组与来源溯源 | 其他平台需不同策略 |
| 打包检查 | `plutil`, `iconutil` | plist 校验、AppIcon 生成 | 原生 Windows 不可运行 |
| 桌面壳 | Info.plist `LSUIElement` | 无 Dock/Terminal Finder app | macOS 专属 |

特别注意：`run_cmd()` 把命令缺失/超时归一为空字符串。`build_state()` 能标记部分组件降级，但 `ps`/`lsof` 完全缺失未必总能形成清晰的 degraded reason；M2 的 PlatformAdapter 必须把 unknown/unsupported 显式类型化。

## 测试保护的安全语义

| 测试区域 | 保护内容 |
| --- | --- |
| `ProcessIdentityTests` | run token/PGID/UID、legacy 全身份、attached 唯一 cwd、非端口认领、任务退出历史 |
| `ProcessLifecycleHardeningTests` | 有界 SIGTERM、手动停止语义、超时保留身份 |
| `KillEndpointTests` | PID 校验、拒绝自身/不存在/非当前用户、显式 force |
| `OperationLockTests` / `AttachConflictTests` | 同应用操作串行、预检不破坏旧服务、创建+认领原子、他卡占用拒绝 |
| `HttpSecurityTests` | DNS rebinding Host、Origin、Cookie、simple-form、CORS/OPTIONS |
| `StaticFileServingTests` | 编码穿越、dotdot、图标路径、符号链接逃逸 |
| `ConfigTests` / `RuntimeStorageTests` | 原子备份、恢复、schema 迁移、未来 schema、只复制旧数据、权限与覆盖边界 |
| `StateTests` | 当前 UID、IPv6 loopback、真实受控身份、外部端口冲突、多卡同端口 |
| `LogTests` / 日志 API 测试 | 轮转、尾读上限、默认 tail |
| `IconTests` | favicon loopback/重定向限制、外部/SVG 拒绝 |
| `FrontendAccessibilityContractTests` | 原子认领 UI、非破坏端口冲突、发现基线、任务状态与健康、键盘/可访问性 |
| `ReleaseFixtureTests` | 敏感数据、绝对路径、符号链接、缓存排除、可复现发行与许可 |

## 已知基线漂移与风险

以下是 M0 发现的现状，不在本 milestone 修复：

1. `AGENTS.md` 写 favicon 支持 SVG；代码仅接受 PNG/JPEG/WebP/ICO，并主动拒绝 SVG。
2. 创建或同时修改 `kind=task` 会清空 port；但对已有 task 单独 `PUT {port: ...}` 可写入非空 port，未全程强制 task 无端口。
3. 已认领 attached/legacy 服务可以显示 `running`，但自动 favicon handler 只看 token 管理的 `managed_pids()`，会反复退避失败。
4. 配置 schema 示例漏列实际持久化的 `glyph`。
5. 文档要求 8 位 hex app id；磁盘加载只要求 id 非空，HTTP 路由仍只匹配 8 位 hex。
6. `AGENTS.md` 把进程详情描述为一次 `ps`；代码实际执行固定列与 args 两次 `ps`，对外语义基本一致。
7. `/api/pick` 选择脚本时还返回推导的 `command`，是文档未列出的向后兼容扩展。
8. M0 的 Windows 运行中，Node 24 的 7 项行为测试直接执行通过，但 `tools/check_project.py` 只解析旧 TAP `# pass N` 文本；完整证据见 `BASELINE.md`。
9. 前端未消费 `/api/health`、`schemaVersion` 和若干细粒度 health/watched 字段；兼容字段仍可能有条件分支，不能据此直接删除。

这些漂移没有在 M0 被解释为新规范；后续修改必须以 SPEC > AGENTS > PLAN 的顺序判断，并在需要改变行为时补测试和文档。

## M1 已保持的边界

- `server.py` 继续可作为兼容入口，HTTP payload 不变。
- 已抽取纯函数、legacy 结构类型、配置与归一化，没有引入 Windows 行为变化。
- 所有身份、停止、认领、配置恢复和 HTTP 安全测试继续作为迁移护栏。
- OS 调用仍留在兼容入口；M2 才通过明确的 `PlatformAdapter` 实现 Windows/macOS，`adcc/` 没有临时 `sys.platform` 分支。
- 前端继续无构建，并保留 `window.__poll`、mutation epoch、keyed reconcile 与旧字段 fallback，除非有独立迁移测试。
