# M0 HTTP API 基线

## 范围

本文记录基线提交 `a5c3adae1f1fa0bd9f0ac7b090ec422e285d0c0f` 的实际 HTTP 路由、前端消费者和跨层契约。它描述现状，不引入 `/api/v1`；版本化 API 属于后续 milestone。

M1 复核：Core 提取未改变本文记录的 endpoint、HTTP status 或 JSON payload；`server.py` 继续提供同一兼容 Interface。

## 通用传输与安全规则

- 服务只绑定 `127.0.0.1`，默认尝试 9600–9609。
- 所有请求必须使用当前控制台端口上的 `127.0.0.1`、`localhost` 或 `::1` Host，客户端地址必须是 loopback。
- 浏览器写请求如果带 `Origin`/`Sec-Fetch-Site`，必须精确同源并带本进程发放的 `console_session` HttpOnly cookie。
- headerless 本地客户端可以调用 JSON API，以兼容本地脚本；仍受 loopback、Content-Type、Content-Length 和请求大小约束。
- JSON body 上限 1 MiB；图标 body 上限 5 MiB；不支持 Transfer-Encoding。
- 不提供 CORS；`OPTIONS` 返回 403。
- JSON 错误通常为 `{"ok": false, "error": "..."}`。
- API 响应和页面响应默认 `Cache-Control: no-store`，并设置 CSP/同源/防 frame 等安全头。
- `Handler` 使用 HTTP/1.1 keep-alive。无业务 body 的 POST 也必须读取前端发送的 `{}`，否则下一请求会被残留字节污染。
- App ID 路由只匹配 8 位十六进制：`/api/apps/<8hex>`。

## 读取接口

| 方法与路径 | Handler | 现有消费者 | 基线行为 |
| --- | --- | --- | --- |
| `GET /api/health` | `server.py:2998` | 无前端消费者 | 不运行 `ps/lsof`；返回版本、schema、目录/配置健康和 degraded/issues |
| `GET /api/state` | `server.py:3001` | `static/app.js:125` | 前端唯一主状态入口；约 2.2 秒服务端缓存，前端每 2 秒轮询 |
| `GET /api/console/log?tail=N` | `server.py:3005,3077` | `static/js/overlays.js:664,692` | 控制台日志尾读；默认 300 行，限制 1–5000 |
| `GET /api/apps/{id}/logs?tail=N` | `server.py:3008,3070` | `static/js/overlays.js:665,692` | 应用日志尾读；同上 |
| `GET /icons/{name}` | `server.py:3015,3051` | 应用卡/图标 `<img>` | basename + 扩展白名单；仅从私有 icons 目录读取 |
| `GET /favicon.ico` | `server.py:2995` | 浏览器 | 返回 `static/assets/favicon.ico` |
| `GET /...` | `server.py:3018,3024` | 浏览器模块/资源 | 映射 `static/`；realpath/commonpath 防穿越；缺首页时返回占位页 |

`GET /api/health` 是守护进程/桌面壳可用的轻量入口，但当前浏览器 UI 不调用它。

## 服务监控与用户偏好写接口

| 方法与路径 | 请求 | Handler | 前端消费者 | 关键语义 |
| --- | --- | --- | --- | --- |
| `POST /api/kill` | `{pid, force?}` | `server.py:3259` | `overlays.js:90`, `launchpad.js:636` | 拒绝非法 PID、控制台自身和非当前 UID；force 选择 `SIGKILL` |
| `POST /api/services/flag` | `{key, flag, value}` | `server.py:3273` | `services.js:145` | flag 仅限 hidden/pinned/promoted；key 是兼容 `name:port`，不是 DOM `instanceKey` |
| `POST /api/watch` | `{keyword, action}` | `server.py:3299` | `services.js:517,665` | action 为 add/remove，返回更新后的 keywords |
| `POST /api/ui/theme` | `{theme}` | `server.py:3207` | `core.js:495` | 校验注册主题并持久化；当前 UI 没有触发持久化主题包切换的调用点 |

## 项目发现与原生选择器

| 方法与路径 | 请求 | Handler | 前端消费者 | 关键语义 |
| --- | --- | --- | --- | --- |
| `POST /api/pick` | `{what: "dir"|"script"}` | `server.py:3168` | `overlays.js:564,590` | 通过 `osascript` 打开 macOS 选择器；取消返回 `{ok:true,canceled:true}`；script 还返回推导的 `command` |
| `POST /api/project/detect` | `{cwd}` | `server.py:3188` | `overlays.js:395` | 只读、根目录有界读取；返回项目名、命中文件和候选命令，不执行项目代码 |

## 控制台自身

| 方法与路径 | Handler | 前端消费者 | 关键语义 |
| --- | --- | --- | --- |
| `POST /api/console/restart` | `server.py:3220` | `static/app.js:310` | 先启动独立 helper 并返回，再延迟 shutdown；优先复用原端口；不停止受控应用 |
| `POST /api/console/stop` | `server.py:3244` | `static/app.js:348` | 先返回，再延迟 shutdown；不停止受控应用 |

两个入口都无业务请求体，但前端通用 `post()` 会发送 `{}`；路由必须保留 `discard_body()`。

## 启动台应用接口

| 方法与路径 | 请求/响应概要 | Handler | 前端消费者 | 关键语义 |
| --- | --- | --- | --- | --- |
| `POST /api/apps` | 创建字段，可带 `attachPid`；返回 app | `server.py:3324` | `overlays.js:494` | create + attach 在同一操作/配置锁内复验并原子保存；失败不留半成品 |
| `PUT /api/apps/{id}` | 部分字段，可带 `stopBeforeUpdate`；返回 app | `server.py:3643` | `overlays.js:493` | 运行中修改生命周期字段默认 409 + `requiresStop`; API 客户端可显式原子停止后更新 |
| `DELETE /api/apps/{id}` | `{ok}` | `server.py:3747` | `launchpad.js:484` | 先安全停止；失败不删配置；成功删除配置、图标和轮转日志 |
| `POST /api/apps/reorder` | `{ids:[...]}` | `server.py:3448` | `launchpad.js:727` | 稳定排序；未列 id 保持相对顺序 |
| `POST /api/apps/{id}/start` | `{ok,pid}` 或错误/health | `server.py:3468` | `launchpad.js:435` | 配置健康预检、真实端口占用检查、per-app 操作锁、持久化 run identity |
| `POST /api/apps/{id}/stop` | `{ok}` | `server.py:3515` | `launchpad.js:435,635`, `overlays.js:444`, `widgets.js:455` | 仅停止已验证 token 组或严格 legacy 身份；不按端口杀；超时 409 |
| `POST /api/apps/{id}/restart` | `{ok,pid}` | `server.py:3550` | `launchpad.js:470`, `app.js:421` | 先预检，避免坏配置先停工作服务；等待正常退出后再启动，不自动 force |
| `POST /api/apps/{id}/diagnose` | `{ok,issues,summary}` | `server.py:3199` | `launchpad.js:662` | 合并静态健康、依赖/日志/退出码规则；不调用外部 AI |
| `POST /api/apps/{id}/attach` | `{pid}` -> `{ok,pid,cwdUpdated?,cwd?}` | `server.py:3529` | `launchpad.js:596` | service + 配置端口 + 当前 UID + 实际监听 + 真实 cwd；锁内拒绝他卡占用 |
| `POST /api/apps/{id}/icon` | PNG/JPEG/WebP 原始字节 | `server.py:3598` | `overlays.js:515` | MIME/大小/魔数校验，0600 私有写入；替换旧用户图标 |
| `DELETE /api/apps/{id}/icon` | `{ok}` | `server.py:3785` | `overlays.js:534` | 删除用户上传图标，保留 glyph/favicon fallback |
| `POST /api/apps/{id}/favicon` | `{}` -> `{ok,favicon}` | `server.py:3407` | `launchpad.js:78` | 从受控进程实际 loopback 端口抓取；仅接受 png/jpg/webp/ico |

start/stop/restart/diagnose/favicon 无业务 body，但必须清空通用前端发送的 `{}`。

## `/api/state` 当前形状

顶层稳定字段：

```text
services[]
watched[]
apps[]
watchedKeywords[]
themes[]
uiTheme
consolePort
consolePid
consoleCwd
version
schemaVersion
degraded
degradedReasons[]
configHealth
```

### `services[]`

主要字段：

```text
key                  # 兼容持久化键 name:port
instanceKey          # DOM/本次进程实例键 pid:port
pid, name, port
openHost
cwd, project, cmd
cpu, mem, uptimeSec
group                # mine | background
pinned, hidden, promoted
appId, appName
origin{label,icon}
```

重要契约：

- 只返回当前 UID，排除控制台自身。
- `instanceKey` 识别同名同端口后来出现的新实例；`key` 继续用于持久化标记。
- `origin` 仅用于展示，不参与启停身份。

### `watched[]`

主要字段为 `pid/name/cmd/cpu/mem/uptimeSec/keyword/keywords`。当前前端只使用兼容单值 `keyword`，没有消费 `keywords[]`。

### `apps[]`

主要字段：

```text
id, name, command, cwd
port, ports[], openHosts{}
emoji, glyph, icon, favicon
kind                 # service | task
running, pid, uptimeSec
attached, legacyManaged
listening
portOccupied, portOccupiedPid, portOwner
portConflict, portConflictApps   # 兼容字段，当前固定 false/[]
lastExit
health{status,blocking,issues[]}
```

重要契约：

- `running` 表示受控身份成立，不表示“配置端口上有任意监听者”。
- `port` 是配置端口；`ports[]` 是受控进程组实际监听端口。
- `portOccupied` 表示配置端口由本卡片身份以外的进程占用。
- 多个定义可保存相同常见开发端口，直到实际启动时才根据真实占用判断。
- task `lastExit.status` 为 `succeeded/canceled/failed/stopped`，旧记录只在 API 输出时兼容归一化，不改写磁盘。
- health 的确定 blocking 问题阻止启动；unknown 不阻止。

## 前端消费与刷新契约

### 唯一主状态输入

`static/app.js` 是 `/api/state` 的唯一直接消费者，再把快照传给：

- `renderLaunchpad(apps)`；
- `renderServices(state)`；
- `renderWidgets(state)`。

其他模块从 `core.js` 的共享 `state.data` 读取；多数写路径成功后调用 `window.__poll()`，少数路径（例如服务表中的显式 kill）依赖下一次定时轮询。

### 并发与旧快照保护

- `core.js` 的 `post/put/del` 每次写入增加 mutation epoch。
- state 请求开始时捕获 epoch；写入发生后返回的旧快照被丢弃并立即重轮。
- 启动/停止会追加延时轮询，以覆盖进程启动、端口监听和退出传播延迟。
- 拖拽/键盘排序期间前端避免轮询重排破坏交互。

### 日志轮询

日志抽屉不依赖主 state 轮询，单独以约 1.5 秒获取有界尾部；页面隐藏时暂停。

## 兼容字段与未消费字段

不能仅以“当前 UI 未显示”为由删除下列字段：

- `services.key` 与 `instanceKey` 各有不同职责；
- `apps.portConflict/portConflictApps` 前端仍保留条件分支；
- 缺失 `kind` 由前端按 service 处理；
- 缺失 `ports[]`、`instanceKey`、新 task status 均有旧数据 fallback；
- `themes[]`/`uiTheme` 保留主题注册协议，即使产品只有 Ops；
- `schemaVersion`、health 细节、`legacyManaged`、`watched.keywords[]` 当前部分或完全未显示，仍可能供外部本地客户端使用。

## M0 发现的跨层偏差

1. 自动 favicon 以 `app.running` 为触发条件，但 handler 只查询 token `managed_pids()`；attached/legacy 可被状态层判定运行，却无法自动获取 favicon。
2. UI 创建 task 时会清 port，后端在创建/改 kind 时也清 port；但对已有 task 单独更新 port 的部分 PUT 没有再次强制清空。
3. 文档称 favicon 支持 SVG；实际抓取层拒绝 SVG。
4. 前端传递 `attachPid` 而不传 `attachInstanceKey`；安全性完全依赖后端在配置锁内重新验证 PID/端口/UID/cwd，这是必须保留的正确边界。
5. `GET /api/health` 当前没有浏览器消费者；未来 Desktop/CLI 接入时应复用，不要在客户端复制健康逻辑。

## 后续版本化约束

M1 不应改变本文件记录的 payload。M4 引入 `/api/v1` 时，应同时保留这些兼容 `/api/...` 路由，直到现有前端完成迁移并有契约测试证明可移除。
