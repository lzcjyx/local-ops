# ADR 0003: 项目域与 legacy apps 配置迁移（schema v2）

- 状态：Accepted
- 日期：2026-08-15
- Milestone：M3

## 背景

M3 要把扁平启动台演进为 Workspace → Project → Resource 分层（SPEC §6、
§9）。现有 `config.json` 是 schema v1 的扁平 `apps` 数组，同时是运行时
受管身份（M2 的 token/PGID/进程树校验）的唯一数据源。直接替换 apps 会
破坏运行时身份；不迁移则无法建立项目视图。

## 决策

### schema v2：legacy 保留 + 新域骨架

- `CURRENT_SCHEMA_VERSION` 升到 2；`CONFIG_DEFAULT` 增加
  `workspaces/projects/resources` 三个空数组。
- `migrate_config_v1_to_v2` 只添加空骨架，**不做**项目填充——填充需要
  真实 cwd 归一化与 id 生成，属于领域逻辑而非存储迁移。
- `apps` 数组原样保留：运行时身份、启停、attach 全部继续读它。

### 惰性幂等填充

- `adcc.projects.registry.assign_resources_from_apps`：仅当
  `resources` 为空且 `apps` 非空时执行；按 `os.path.realpath(cwd)`
  分组建项目，cwd 缺失/无效的进固定 `Unassigned` 桶
  （id `00000000`，名称「未分配」），绝不丢弃。
- 每个 resource 写入 `app_id` 桥字段，供状态投影把运行时身份
  （app 的 running/ports）映射回项目。
- 由 `server._run_console` 启动时调用一次（`ensure_project_domain`），
  异常只记日志不阻断启动；重跑幂等。

### 项目视图

- `build_project_summaries`：`state.projects` 摘要数组
  （id/name/rootPath/repoPath/tags/resourceCount/runningCount），
  不改变任何 legacy 字段。
- `build_apps` 每行增加 `projectId/projectName`（经 `app_id` 桥）。
- 前端启动台过滤芯片动态追加项目项（按项目集合缓存 defs，
  项目被删时重置回「全部」）。

### 检测

- `adcc.projects.detection.git_root`：只读 `git rev-parse
  --show-toplevel`，非仓库/失败返回 None（不猜测）。
- `detect_mcp_servers`：只读解析 `.mcp.json` 与 `package.json` 的
  `mcp` 字段，产出 `kind: mcp_server` 候选；`server.detect_project`
  合并这些候选并返回 `repoPath`。

## 结果

- 既有 v1 配置加载即升 v2（`.bak` 保留原文，幂等不重写）。
- 同 cwd 的 legacy apps 聚为一个项目；无 cwd 任务进 Unassigned。
- 多项目可保存相同端口（registry 不限制），运行时身份仍由 apps 的
  token/PGID 判定，与项目分组无关。
- 165 项测试通过（含 20 项新项目域测试）；`check_project --skip-tests`
  全绿。

代价与限制：

- `resources` 是定义副本：CRUD 落 API 与真正的资源启停（走 ManagedRun）
  属 M4；M3 期间删除/编辑资源不会同步 apps（运行时仍安全）。
- 项目摘要是 M4 前的只读投影；`Unassigned` 桶固定 id 是保留命名空间。

## 未采用方案

- 迁移器内直接填充项目：把领域规则（cwd 分组、id 生成）塞进存储层，
  破坏可测性与关注点分离。
- 删除 apps 数组：破坏 M2 运行时身份，违背 strangler 原则。
- 独立 projects.json 文件：SPEC §13.1 要求同一版本化 JSON 存储类，
  且双文件迁移/备份协议更复杂。
