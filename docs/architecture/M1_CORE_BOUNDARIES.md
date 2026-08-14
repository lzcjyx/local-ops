# M1 Core 边界

## 结论

M1 将配置持久化、运行时文本归一化、受管身份策略和 task 退出策略提取到 `adcc/`。`server.py` 仍是兼容入口和当前 macOS 实现宿主，但不再保存这些规则的第二份实现。HTTP 路由、JSON payload、前端和配置 schema 均未改变。

```text
HTTP / legacy Python callers
            |
            v
server.py compatibility Interface
  |                     |
  | macOS facts/I/O     | cache + private-dir callbacks
  v                     v
adcc.runtime.*       adcc.storage.config
  |                     |
  +---- OS-neutral -----+
            |
            v
      legacy-compatible data
```

## Module 与 Interface

| Module | 稳定 Interface | 隐藏的 Implementation |
| --- | --- | --- |
| `adcc.core.constants` | schema/theme/run-token/task 常量、当前 config/app defaults | 常量的单一来源 |
| `adcc.core.errors` | `ConfigSchemaError`, `FutureConfigSchemaError` | 配置错误层级 |
| `adcc.core.models` | 当前进程、listener、origin、last-exit 结构类型 | 不提前定义 M3/M4 领域对象 |
| `adcc.storage.config` | `Config.snapshot/health_info/update`, `migrate_config` | 深拷贝、逐版迁移、`.bak` 恢复、只读保护、`fsync` + `os.replace` |
| `adcc.runtime.ports` | `validate_port`, `parse_lsof_listeners`, `listener_open_host` | listener 地址与 legacy set 兼容规则 |
| `adcc.runtime.processes` | `parse_*`, `classify_group`, `project_name`, `attribute_origin` | BSD/macOS 文本归一化与展示策略；不运行命令 |
| `adcc.runtime.lifecycle` | managed/legacy/attached identity 与 owner resolution | token + PGID + UID、端口 + UID + real cwd、唯一匹配规则 |
| `adcc.runtime.tasks` | `classify_task_exit`, `public_last_exit` | task 四态输出兼容策略 |

这些 Module 的 Depth 来自完整封装现有策略，而不是按函数数量拆文件。配置持久化的调用者无需知道备份与恢复协议；运行时策略的调用者只提供已采集 facts。

## `server.py` 保留的职责

- 运行 `ps`、`lsof`、`osascript` 和 `/bin/bash`；
- `fcntl` 单实例锁、UID/PGID、信号、进程启动与停止；
- HTTP 安全、路由、payload orchestration、状态缓存；
- macOS PATH、Finder launcher 与 console restart；
- legacy runtime 目录迁移、图标、日志和 favicon I/O。

这些都是 M2 `PlatformAdapter` 的输入边界或更高层 application orchestration，不在 M1 创建临时平台分支。

## 兼容包装

- `server.Config` 继承 Core `Config`，只注入 `invalidate_state_cache`、logger 和现有私有目录策略；
- `scan_listeners`、`ps_snapshot`、`lsof_cwds`、`origin_snapshot`、`pgid_members_map` 保留命令采集，再委托 parser；
- `managed_process_index` 与 `legacy_managed_pid` 保留 facts 采集，再委托 identity policy；
- `classify_task_exit`、`public_last_exit`、`validate_port` 等纯函数由 `server.py` 直接 re-export；
- `listener_app_owners` 在 `cwds` 未预采集时仍由 wrapper 补齐 facts，Core 不执行文件系统或进程查询。

## 保持的不变量

1. 新受管身份仍要求 PGID + 当前 UID + matching run-token controller。
2. legacy/attached 仍要求配置端口 + 当前 UID + real cwd；attached PID 轮换必须唯一。
3. 同一 PID 命中多张卡片时不建立 owner。
4. 端口占用仍不是进程所有权或停止权限。
5. 配置仍先写上一份良好 `.bak`，再原子替换主文件；未来 schema 不降级覆盖。
6. task legacy `lastExit` 只在 API 输出副本上归一化，不改写磁盘。
7. `server.*` 入口、HTTP status/error/payload 和前端消费契约不变。

## 发行与检查

发行 allowlist 已加入 `adcc/`，项目语法检查会递归解析其中的 Python 文件；发行测试明确断言配置与生命周期 Module 被打包，避免生成一个只能找到 `server.py`、却无法 import Core 的损坏发行物。

## 后续边界

下一 milestone 才定义 `PlatformAdapter`、typed capability/error 和 Windows/macOS 实现。M1 不宣称原生 Windows 支持；Windows 上现有 `fcntl` 导入、macOS 工具检查和 Node 24 TAP 汇总器问题仍按 M0 基线记录。

