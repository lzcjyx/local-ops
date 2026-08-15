# ADR 0010: 控制中心 GUI（M10 多视图）

- 状态：Accepted
- 日期：2026-08-15
- Milestone：M10

## 背景

SPEC §19.2/§26 要求 GUI 完成首发旅程（加项目、看服务、启停、Agent、
工作流、日志）。约束：不迁移前端框架（SPEC §19.1 + PLAN M10 约束）。

## 决策

### 导航扩展（布局 v2 框架内）

- rail 导航轨由 2 视图扩展为 6 视图：启动台 / 服务监控 / **概览** /
  **项目** / **Agent** / **工作流**；日志中心与设置中心保留弹层
  （data-action）。
- `app.js` 视图切换改为 `VIEWS` 元数据表驱动（title/overline/sub/
  section），`switchView/applyView` 不再硬编码两个视图。
- 新视图是 `.view` section（复用既有布局/动效/aria 契约）。

### 新模块 `static/js/views.js`（原生 ES Module）

- 概览：项目/Agent 会话/运行服务/失败工作流/端口冲突/daemon 健康
  KPI + 「需要关注」列表。
- 项目：卡片网格（资源数/运行数/Git 根/会话/工作流）+ 资源一键
  启停（经 `/api/v1/resources/{id}/start|stop`）。
- Agent：会话列表（适配器名/状态/PID/耗时/退出码）+ 停止 + 新建
  会话模态（适配器/项目/prompt）。
- 工作流：定义卡片（步骤链/最近运行/步骤状态/重试计数）+ 运行/取消。
- 数据：概览/项目来自 `/api/state`（app.js 传入）；会话/工作流/
  worktrees 来自 `/api/v1`，2.5s TTL 节流拉取（仅渲染时触发，
  失败静默降级）。

### 样式

- 结构样式加在 `static/base.css`「布局 v2」段之后（主题令牌驱动，
  视觉由 ops.css 皮肤）；新类全部使用既有 CSS 变量。

## 结果

- 前端 18 项测试 + check_project（JS 语法/绑定）全绿；真实 daemon
  页面与全部 JS 模块加载 200。
- exit gate：首发旅程（SPEC §26 1-13 项）全部可在 GUI 完成——桌面
  壳（M9）承载同一 UI，无需 CLI。

代价与限制：

- 工作流视图是结构化列表（PLAN 允许，非可视化 DAG 编辑器）；
- v1 数据是轮询近似（2.5s TTL），非 SSE 实时（事件流已就绪，前端
  接入留作优化）。

## 未采用方案

- React/Vue/Svelte 迁移：违反约束，无收益。
- 每个视图一个模块文件：views.js 内聚渲染 + 共享 v1 缓存更简单。
