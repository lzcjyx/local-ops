# ADR 0012: P1 功能交付

- 状态：Accepted
- 日期：2026-08-16
- Milestone：P1（SPEC §4.2）

## 决策

### Linux 平台适配器

- `adcc/platform/linux.py`：继承 macOS 的 POSIX 控制原语（信号/
  进程组/fcntl/flock），替换事实采集为 Linux 原生（ps/lsof），
  cwd 用 `/proc/<pid>/cwd` 回退；`osascript`/Keychain 依赖覆盖为
  typed 降级。测试 `skipUnless(linux)`，CI 无 Linux runner 时以
  macOS 语义守卫（两者 POSIX 同源）。

### 远程只读面板

- `CONSOLE_REMOTE_READONLY=1` 显式启用（默认仍只绑定 127.0.0.1）；
  启用后 `CONSOLE_BIND_HOST` 可绑定非回环地址，Host 检查放宽，
  **全部写操作拒绝**（403），读操作维持 token/cookie 姿态。
- 测试：只读模式 GET 放行、POST 403。

### 项目 secrets（OS 凭据库）

- `adcc/core/secrets.py`：Windows Credential Manager
  （advapi32 CredReadW/CredWriteW，ctypes 零依赖）；macOS Keychain
  （`security` CLI）；Linux typed unsupported。
- 环境值支持 `${secret:<name>}` 语法，启动时解析（AgentRunner），
  明文不落配置。Windows 往返真实测试通过。

### 项目模板与 manifest

- `adcc/projects/templates.py`：4 个预设模板（web-frontend/
  python-api/static-site/mcp-server），按名幂等应用。
- Manifest v1：项目+资源 JSON 导出/导入，按 id 幂等合并（跳过
  已存在）。
- API：`GET /api/v1/project-templates`、`GET /api/v1/projects/export`、
  `POST /api/v1/projects/import`、`POST /api/v1/projects/{id}/template`、
  创建项目可带 `template`。

### Agent capability discovery 与 cost 元数据

- `adcc/agents/discovery.py`：探测 PATH 上的常见编码 Agent
  （opencode/codex/claude/gemini/aider/goose/qwen/cursor），提供
  建议适配器；`GET /api/v1/agents/discovery` +
  `POST /api/v1/agents/discovery/register`。
- 适配器新增 `cost`（{model, inputPer1k, outputPer1k}）与
  `token_budget` 元数据（展示用，不参与调度）；UI 会话卡展示。

### worktree 自动分配

- AgentRunner：适配器 argv/cwd 模板含 `{worktree_path}` 时，自动
  在项目 Git 仓库创建 ADCC 命名空间 worktree
  （`adcc/<session>/<id8>`）并注入变量；创建失败则启动报错（不
  静默降级）。

### UI 创建入口补全

- 项目视图「新建项目」（名称/路径/模板）、Agent 视图「注册适配器」
  （名称/可执行/参数/环境模板）与「新建会话」（适配器/项目/提示词）、
  工作流视图「新建工作流」（步骤 JSON，DAG 校验提示）；会话卡
  「日志」展开读取 `/api/v1/agents/sessions/{id}/logs`（新增路由）。
- 桌面壳加入开机自启（tauri-plugin-autostart，仅 macOS 使用
  LaunchAgent 参数，Windows 走注册表）。

### 未实现（文档化）

- 本地插件 SDK：command 适配器已覆盖任意命令，插件接口留待
  实际需求；自动更新需签名/更新服务器（发布基础设施）。

## 结果

- 新增 `tests/test_p1.py` 11 项（模板/manifest/discovery/cost/secrets/
  只读）；全量 262 项通过；前端 18 项 + 页面加载全绿；桌面壳编译
  通过（autostart 插件）。
