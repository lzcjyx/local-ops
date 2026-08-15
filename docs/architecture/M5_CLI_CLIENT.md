# M5 CLI 客户端

## 结论

`adcc` CLI 是 daemon 的薄客户端（`adcc/cli/main.py`），全部命令经
`/api/v1`，不重复实现运行时逻辑。端点发现通过 `DATA_DIR/daemon.json`
（启动写入、停止删除，0600）。详见 `docs/adr/0005`。

```text
adcc <command> ──HTTP──> daemon (127.0.0.1:<port>) ──> /api/v1
     │                        ▲
     └── daemon.json ◄───────┘ (port/pid/token)
```

## 退出码（稳定契约）

| 码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 1 | 业务/请求错误（daemon 已响应） |
| 2 | 用法错误（argparse） |
| 3 | daemon 不可达（无端点文件/连接失败/端口非法） |

## 命令覆盖

status / doctor / projects list|show / resources list / start /
stop / restart / ports / port owner / runs list / logs（--follow）。

## 关键修复（M5 里程碑）

- v1 启停 discard body（keep-alive 陷阱）。
- Windows CIM 缓存：新进程启动后 `invalidate_cache()`，restart 后身份
  立即可见（否则 stop 误报「未在运行」且进程残留）。
- CLI 判定成功 = `status==200 && ok`。
- 新 app 创建/删除同步注册/清理项目资源（app_id 桥闭环）。

## Exit gate 验证

```text
adcc status --json                 ✓ 退出码 0
adcc projects list --json          ✓
adcc start <resource-id>           ✓
adcc logs <run-id>                 ✓
adcc port owner <port> --json      ✓（无监听者时退出码 1）
```

以上在真实 daemon 上契约测试通过；无 daemon 时退出码 3。
