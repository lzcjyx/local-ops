# M2 Platform 边界与 Windows 运行时

## 结论

M2 把全部 OS 事实采集与控制原语移入 `adcc/platform/` 的
`PlatformAdapter`（macOS / Windows / unsupported 占位），`server.py`
不再直接调用 `ps`、`lsof`、`osascript`、`fcntl`、PGID/信号与
`os.getuid`，改为 adapter 薄包装，Windows 上可导入并运行完整
启停/监控/身份链路。详见 `docs/adr/0002`。

```text
HTTP / legacy callers
        |
        v
server.py (compat wrappers, cache, HTTP, orchestration)
        |
        v
adcc.platform.<PlatformAdapter>       <- 采集 facts / 控制原语 / 锁 / 对话框
        |                    |
        v                    v
adcc.runtime.ports       adcc.runtime.processes / lifecycle / tasks
   (纯解析)                 (纯策略)
```

## Adapter 能力

| 能力 | macOS | Windows | unsupported |
| --- | --- | --- | --- |
| 用户身份 | `getuid` 数值 | 用户名（SessionId 判定 owner） | typed error |
| 进程快照 | `ps -axo pid,uid,etime,cpu,mem,comm` ×2 | CIM 全量（TTL 2s） | typed error |
| 溯源表 | `ps -axo pid=,ppid=,args` | CIM ParentProcessId | typed error |
| 进程组 | `ps pid,pgid` + killpg | 无（树语义，`taskkill /T`） | typed error |
| cwd | `lsof -d cwd` | `{}`（unknown 降级） | typed error |
| 监听端口 | `lsof -iTCP -sTCP:LISTEN` | `netstat -ano` 解析 | typed error |
| 受管身份 | PGID + UID + argv token | lastPid + 用户名 + cmdline token + 树 | — |
| 停止 | SIGTERM 优雅，超时不升级 | 优雅尝试 → 控制台进程自动升级 /F | typed error |
| 单实例锁 | `fcntl.flock` | `msvcrt.locking` | typed error |
| 目录/文件选择 | `osascript` | PowerShell WinForms（STA） | typed error |
| PATH 注入 | Homebrew/nvm/fnm 等 | 继承当前 PATH | typed error |
| launcher 对话框 | osascript dialog/alert | None（降级） | typed error |

## Windows 关键决策

- 受管启动包装：`cmd.exe /d /s /c "set "CONSOLE_RUN_TOKEN=<token>" && <command>"`
  —— 令牌进入可查询命令行，身份校验不再依赖进程组。
- `os.kill(pid, 0)` 在 Windows 是 TerminateProcess（会杀目标），
  `pid_alive` 改用 ctypes `OpenProcess` + `GetExitCodeProcess`。
- cwd 不可得 ⇒ legacy/attached 身份在 Windows 不建立；外部端口占用者
  只报告占用，绝不认领/停止（exit gate 已测）。
- 首次 state 构建 ~1.7s（一次 CIM 全量），TTL 缓存命中后 ~10ms。

## 保留的不变量

1. 新受管身份仍要求 token + 当前用户（macOS 加 PGID；Windows 加进程树）。
2. legacy/attached 仍要求端口 + UID + cwd 四重校验（Windows 因 cwd
   unknown 自动不可用，安全默认）。
3. 端口占用仍不是进程所有权或停止权限。
4. 停止超时仍不自动 SIGKILL；force 仍显式。
5. `server.*` 入口、HTTP payload 与前端契约不变。
6. macOS 测试类在 Windows 上 skip，macOS CI 继续执行。

## 后续边界

- M2 CI：fork 仓库 Actions 被禁用，Windows/macOS CI 矩阵待启用后补充
  （台账记录）。
- Linux adapter 保持 unsupported typed error（P1）。
- 认领（attach）在 Windows 的 cwd 替代方案（如 PowerShell 采样 cwd）
  属后续优化，不是 P0。
