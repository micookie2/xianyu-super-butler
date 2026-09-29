"""物流 Agent 路由（占位实现）。

与 ``logistics_quote`` 同样的原因：上游引用的模块从未发布。前端产物里没有
任何 ``/api/logistics/agent`` 调用，因此这里只保留路由工厂本身，让服务能
装配起来，不对外暴露任何端点 —— 注册一堆必然 404 的假路由只会掩盖真实问题。

原始实现恢复后删除本占位即可。
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter


def create_logistics_agent_router(
    get_current_user: Callable[..., dict[str, Any]],
    db_manager: Any,
) -> APIRouter:
    """返回一个空的 Agent 路由（功能未随本次部署发布）。"""
    return APIRouter(prefix="/api/logistics/agent")


__all__ = ["create_logistics_agent_router"]
