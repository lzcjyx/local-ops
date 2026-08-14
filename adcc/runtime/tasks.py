"""Pure task exit-status compatibility helpers.

These functions deliberately preserve the legacy ``server.py`` semantics while
keeping task outcome policy independent from HTTP, storage, and platform code.
"""

from adcc.core.constants import TASK_CANCELED_EXIT_CODE


def classify_task_exit(code):
    """把一次性任务的退出码归一为稳定的产品语义。"""
    if code == 0:
        return "succeeded"
    if code == TASK_CANCELED_EXIT_CODE:
        return "canceled"
    return "failed"


def public_last_exit(app):
    """兼容旧配置：只在 API 输出时补齐任务状态，不改写磁盘。"""
    value = app.get("lastExit")
    if not isinstance(value, dict):
        return value
    result = dict(value)
    if (app.get("kind") or "service") == "task":
        # 旧版把“总控台按钮停止”记作 canceled + null；新协议中它是 stopped。
        if result.get("status") == "canceled" and result.get("code") is None:
            result["status"] = "stopped"
        elif (result.get("status") not in
              {"succeeded", "canceled", "failed", "stopped"}
              and isinstance(result.get("code"), int)):
            result["status"] = classify_task_exit(result["code"])
    return result
