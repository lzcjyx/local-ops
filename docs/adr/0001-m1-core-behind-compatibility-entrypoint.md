# ADR 0001: 在兼容入口后提取 Core

- 状态：Accepted
- 日期：2026-08-14
- Milestone：M1

## 背景

M1 要求把 `server.py` 从唯一实现位置变成模块化 Core 的兼容入口，同时保持现有 HTTP、前端、macOS 行为和安全测试不变。现有测试大量 monkeypatch `server.*`；进程启动、`ps`/`lsof`、PGID、信号与 `fcntl` 又是当前 macOS 实现的一部分，而 M2 才负责 `PlatformAdapter`。

如果在 M1 直接移动实时 OS 调用或让 Handler 直接调用新模块，会同时破坏现有测试 Seam、改变采样顺序，并提前进入 M2。

## 决策

采用 strangler 方式，在 `server.py` 兼容 Interface 后提取 OS-neutral 深 Module：

- `adcc.core` 维护当前 schema、运行身份常量、错误和 legacy 结构类型；
- `adcc.storage.config.Config` 完整拥有 schema 迁移、恢复、只读保护、备份和原子持久化；
- `adcc.runtime.ports` 与 `adcc.runtime.processes` 解析已采集文本并执行展示归一化；
- `adcc.runtime.lifecycle` 只根据已采集 facts 判定 token/PGID/UID、legacy/attached 身份和 listener 所有者；
- `adcc.runtime.tasks` 维护 task 退出状态兼容策略；
- `server.py` 保留原函数名作为兼容 Interface，负责 macOS 命令、实时采样、信号、HTTP 和 cache 注入，再委托上述 Module。

配置的 cache 失效通过 `on_change` callback 注入；私有目录策略通过 `ensure_private_dir` 注入。Core 不反向 import `server.py`。

## 结果

正向结果：

- 配置、端口/进程解析和受管身份策略可在不构造 HTTP server 的情况下调用与测试；
- 现有 `server.*` monkeypatch Seam、HTTP payload 和 macOS 操作顺序保持稳定；
- M2 可以让 `PlatformAdapter` 产生相同 facts，而不重写这些策略；
- `server.py` 不保留已提取逻辑的第二份实现。

代价与限制：

- M1 后 `server.py` 仍包含 `fcntl`、`ps`/`lsof`、`/bin/bash`、PGID 和信号等平台实现；Windows 支持仍明确属于 M2；
- 兼容 wrapper 暂时增加一层调用；这是保护现有入口和测试 Locality 的有意取舍；
- 当前 legacy schema 仍是普通 mapping；M3/M4 才引入 Project、ResourceDefinition 和 ManagedRun 等目标领域模型。

## 未采用方案

- 在 M1 创建跨平台 `PlatformAdapter`：违反 PLAN 的 milestone 顺序。
- 一次搬走 start/stop/attach/HTTP orchestration：改动面过大，且会改变安全关键时序。
- 在 `server.py` 与 `adcc/` 各保留一份逻辑：会产生双实现漂移，降低 Leverage。
- 让 Core import `server.py` 取得 logger/cache/目录：会形成循环依赖并破坏独立可测性。

