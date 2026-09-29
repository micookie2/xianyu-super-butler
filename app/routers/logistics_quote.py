"""物流报价单解析路由（占位实现）。

上游提交 39184ba 引入了 ``app.routers.logistics_quote``，但该模块从未进入
仓库的任何分支、标签或历史提交，``:latest`` 镜像因此直接
``ModuleNotFoundError`` 起不来。这里补一个形状正确的占位实现：

* 路由工厂签名与 ``delivery_block`` 一致，服务能正常装配；
* 前端唯一会真实调用的四个端点返回 503 + 明确原因，而不是 404 让人误以为
  是权限或参数问题；
* 不注册任何未被前端引用的假端点，避免"看起来能用其实不能用"。

原始实现恢复后，直接删除本文件同名占位、把真实模块加回来即可。
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

_NOT_INSTALLED = {
    "success": False,
    "code": "logistics_quote_not_installed",
    "message": "物流报价单解析模块未随本次部署发布，功能暂不可用。",
}

STATUS_CODE = 503


def create_logistics_quote_router(
    get_current_user: Callable[..., dict[str, Any]],
    db_manager: Any,
) -> APIRouter:
    """返回已鉴权、但只提供"未安装"提示的报价单路由。"""
    router = APIRouter(prefix="/api/logistics")

    def unavailable() -> JSONResponse:
        # 仍然挂鉴权依赖：未登录时先得到 401，不泄露功能存在性差异。
        return JSONResponse(status_code=STATUS_CODE, content=_NOT_INSTALLED)

    router.add_api_route(
        "/quote-sources/parse", unavailable, methods=["POST"],
        dependencies=[Depends(get_current_user)],
    )
    router.add_api_route(
        "/quote-books", unavailable, methods=["GET"],
        dependencies=[Depends(get_current_user)],
    )
    router.add_api_route(
        "/quote-books", unavailable, methods=["POST"],
        dependencies=[Depends(get_current_user)],
    )
    router.add_api_route(
        "/quote-books/{book_id}", unavailable, methods=["DELETE"],
        dependencies=[Depends(get_current_user)],
    )
    return router


__all__ = ["create_logistics_quote_router"]
