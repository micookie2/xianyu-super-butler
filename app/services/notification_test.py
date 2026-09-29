"""账号通知规则的"测试发送"服务。

前端（通知与日志 → 规则列表 → 测试）需要对一条规则绑定的渠道发出一次真实
消息，用来验证配置是否可用。这里的职责是把三件事收在一处：

1. 校验规则归属并取出发送目标（``db_manager.get_notification_test_target``
   刻意不按启用状态过滤，这样停用规则也能排查连通性）；
2. 组装一条可辨识的测试文案，交给 ``NotificationSender`` 真实发出；
3. 把渠道层的各种异常压成稳定的错误码 + 中文提示，并对同一用户限频
   —— 测试按钮会真的打到第三方服务，不限频等于给了一个免费轰炸入口。
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Optional

from loguru import logger

from .notification_channels import NotificationChannelConfigError
from .notification_sender import (
    NotificationSendError,
    NotificationSendReceipt,
    NotificationSender,
)


class NotificationTestError(RuntimeError):
    """对外可见的测试发送失败，携带 HTTP 状态码与稳定的错误码。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "notification_test_failed",
        status_code: int = 400,
        retry_after: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after

    def detail(self) -> Dict[str, Any]:
        """响应体形状由前端 getNotificationTestErrorMessage 约定：detail.message。"""
        return {"code": self.code, "message": self.message}


class NotificationTestRateLimiter:
    """按用户滑动窗口限频，进程内内存实现。

    默认每分钟 10 次。测试发送面向的是人工点击，正常操作远达不到这个量；
    超出后抛出带 ``retry_after`` 的错误，前端可据此提示稍后再试。
    """

    def __init__(self, *, limit: int = 10, window_seconds: float = 60.0) -> None:
        self.limit = max(1, int(limit))
        self.window_seconds = max(1.0, float(window_seconds))
        self._hits: Dict[str, Deque[float]] = {}

    def check(self, key: Any) -> None:
        identity = str(key or "anonymous")
        now = time.monotonic()
        bucket = self._hits.setdefault(identity, deque())
        cutoff = now - self.window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self.limit:
            wait = max(1, int(self.window_seconds - (now - bucket[0])) + 1)
            raise NotificationTestError(
                "测试发送过于频繁，请稍后再试",
                code="notification_test_rate_limited",
                status_code=429,
                retry_after=wait,
            )
        bucket.append(now)
        # 长时间不活跃的用户不应无限占用内存。
        if len(self._hits) > 1024:
            for stale in [k for k, v in self._hits.items() if k != identity and not v]:
                self._hits.pop(stale, None)

    def reset(self) -> None:
        self._hits.clear()


notification_test_rate_limiter = NotificationTestRateLimiter()


class NotificationTestService:
    """执行一条通知规则的测试发送。"""

    def __init__(
        self,
        db_manager: Any,
        *,
        sender: Optional[NotificationSender] = None,
        limiter: Optional[NotificationTestRateLimiter] = None,
    ) -> None:
        self.db = db_manager
        self.sender = sender or NotificationSender()
        self.limiter = limiter or notification_test_rate_limiter

    async def send_rule_test(
        self,
        rule_id: int,
        user_id: int,
        user_info: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        target = self.db.get_notification_test_target(rule_id, user_id)
        if not target:
            raise NotificationTestError(
                "通知规则不存在或无权限访问",
                code="notification_rule_not_found",
                status_code=404,
            )

        self.limiter.check(f"{user_id}:{rule_id}")

        request_id = uuid.uuid4().hex[:16]
        started = time.monotonic()
        message = self._build_message(target)

        try:
            receipt = await self.sender.send(
                target.get("channel_type"),
                target.get("channel_config"),
                message,
                request_id=request_id,
            )
        except NotificationChannelConfigError as exc:
            # 配置本身有问题，属于用户可自助修复的 400。
            raise NotificationTestError(
                str(exc) or "通知渠道配置无效",
                code=getattr(exc, "code", "notification_config_invalid"),
                status_code=400,
            ) from exc
        except NotificationSendError as exc:
            raise NotificationTestError(
                exc.public_message,
                code=exc.code,
                status_code=getattr(exc, "status_code", None) or 502,
            ) from exc

        sent_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            f"source=notification_test rule_id={rule_id} request_id={request_id} "
            f"channel_type={target.get('channel_type')} result=success"
        )
        return {
            "success": True,
            "message": "测试消息已发送，请到目标渠道查收",
            "request_id": request_id,
            "channel": {
                "id": target.get("channel_id"),
                "name": target.get("channel_name"),
                "type": target.get("channel_type"),
            },
            "sent_at": sent_at,
            "duration_ms": duration_ms,
            "provider_status_code": getattr(receipt, "status_code", None),
        }

    @staticmethod
    def _build_message(target: Dict[str, Any]) -> str:
        """测试文案带来源标记，方便在渠道侧和真实通知区分。"""
        rule_name = target.get("name") or f"规则{target.get('id')}"
        account = target.get("cookie_id") or "-"
        enabled = "启用" if target.get("enabled") else "停用"
        return (
            "【闲鱼超级管家 · 通知测试】\n"
            f"规则：{rule_name}（当前{enabled}）\n"
            f"账号：{account}\n"
            f"渠道：{target.get('channel_name')} / {target.get('channel_type')}\n"
            "收到本条消息说明该通知渠道配置可用。"
        )


__all__ = [
    "NotificationTestError",
    "NotificationTestRateLimiter",
    "NotificationTestService",
    "notification_test_rate_limiter",
]
