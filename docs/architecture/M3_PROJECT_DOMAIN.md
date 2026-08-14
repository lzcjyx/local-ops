# M3 项目域与配置迁移

## 结论

M3 引入 Workspace → Project → Resource 三层模型（`adcc/projects/`），
通过 schema v2 迁移把 legacy 扁平 `apps` 无丢失地映射进项目结构，
同时保留 `apps` 作为运行时身份数据源（M2 语义不动）。项目视图以
只读投影（`state.projects` + apps 行 `projectId/projectName`）暴露，
前端启动台可按项目过滤。详见 `docs/adr/0003`。

```text
config.json (schema v2)
├─ apps[]            <- 运行时身份（M2 语义不变）
├─ workspaces[]      <- 默认工作区（id 00000001）
├─ projects[]        <- 项目（含 Unassigned 桶 id 00000000）
└─ resources[]       <- 项目资源定义（service/task/mcp_server，app_id 桥）
```

## 领域模型

| 模型 | 关键字段 | 校验 |
| --- | --- | --- |
| Workspace | id, name, project_ids | id 8hex；name 非空 |
| Project | id, workspace_id, name, root_path, repo_path, environment, tags | root_path 必填；repo_path 可空 |
| ResourceDefinition | id, project_id, name, kind, command, cwd, port, environment | kind ∈ service/task/mcp_server；task 强制 port=null |

## 迁移规则（SPEC §9.2）

1. v1 → v2 只加骨架（纯存储迁移，不产生领域规则）。
2. `assign_resources_from_apps` 惰性填充：仅 resources 空且 apps 非空。
3. 按 realpath(cwd) 分组；cwd 无效 → Unassigned 桶；id 用 secrets 8hex。
4. resource 写 `app_id` 桥；apps 数组与运行身份原样保留。
5. 幂等：重跑直接返回 False；`.bak` 保留迁移前原文。

## 状态投影

- `state.projects[]`：id/name/rootPath/repoPath/workspaceId/tags/
  resourceCount/runningCount（running 经 app_id 桥从 apps 的受管判定）。
- apps 行新增 `projectId/projectName`。
- 前端：启动台服务/任务分区过滤芯片动态追加「项目」项（defs 按项目
  集合缓存；项目删除时过滤重置为「全部」）。

## 检测

- `git_root(path)`：只读 git 查询 → repo 根或 None。
- `detect_mcp_servers(root)`：`.mcp.json` / `package.json.mcp` →
  `kind: mcp_server` 候选；并入 `/api/project/detect` 响应，同时返回
  `repoPath`。

## Exit gate 验证

- 既有配置迁移无数据丢失：v1 fixtures 加载 → v2 + 项目/资源全量保留
  （含 Unassigned），`.bak` 原文备份，幂等重跑不重复。
- 多项目共享端口：registry 允许，测试覆盖。
- 运行时身份独立于分组：启停/端口判定仍全部走 apps。
- UI 按项目过滤：芯片 + match 逻辑 + 动态 defs 测试通过
  （test_frontend.js 绑定检查通过）。
