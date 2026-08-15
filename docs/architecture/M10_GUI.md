# M10 控制中心 GUI

## 结论

rail 导航扩展为 6 视图（启动台/服务监控/概览/项目/Agent/工作流），
日志与设置保留弹层；新视图由 `static/js/views.js` 原生实现，不迁移
框架。详见 `docs/adr/0010`。

```text
rail: 启动台 | 服务监控 | 概览 | 项目 | Agent | 工作流 | (日志) (设置)
        │
        ▼
app.js VIEWS 表驱动 switchView/applyView
        │
        ├─ /api/state  → 概览/项目（app.js 传入）
        └─ /api/v1     → Agent 会话/工作流/worktrees（views.js 2.5s TTL）
```

## 视图能力

| 视图 | 内容 |
| --- | --- |
| 概览 | 项目/会话/服务/失败工作流/端口冲突/daemon 健康 KPI + 关注列表 |
| 项目 | 卡片（资源/运行/Git/会话/工作流）+ 资源一键启停 |
| Agent | 会话列表（状态/PID/耗时/退出码）+ 停止 + 新建（适配器/项目/prompt） |
| 工作流 | 定义卡片（步骤链/最近运行/步骤状态/重试）+ 运行/取消 |
| 日志/设置 | 既有弹层（未改动） |

## Exit gate

SPEC §26 首发旅程 1-13 项全部可在 GUI 完成（桌面壳 M9 承载同一 UI）。
