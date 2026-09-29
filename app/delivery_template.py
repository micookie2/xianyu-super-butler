"""发货内容发送器：把一条卡券内容变成买家实际收到的若干段消息。

发货内容（``_auto_delivery`` 的返回值）有两种形态：

1. 纯文本卡券，可能超过闲鱼单条消息的长度上限，需要分段；
2. ``__IMAGE_SEND__`` 前缀的图片标记，形如
   ``__IMAGE_SEND__{card_id}|{image_url}`` 或 ``__IMAGE_SEND__{image_url}``
   （旧格式没有卡券 ID），需要走图片消息通道。

历史上这段分支直接写在 ``app.reply_server`` 的自动发货循环里。抽出来之后，
回复服务只关心"发出去了几段"，协议细节集中在这里。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, List

from loguru import logger


#: 图片发货内容的标记前缀，与 XianyuAutoAsync 生产侧保持一致。
IMAGE_SEND_PREFIX = "__IMAGE_SEND__"

#: 闲鱼单条文本消息的长度上限，与 ``send_im_text`` 的校验一致。
MAX_SEGMENT_LENGTH = 2000

#: 一条卡券被拆成多段时的发送间隔，避免触发发送频率风控。
SEGMENT_INTERVAL_SECONDS = 1.0


def split_text(text: str, limit: int = MAX_SEGMENT_LENGTH) -> List[str]:
    """按平台长度上限切分文本，保留换行，不做任何内容改写。

    卡券码之间以换行分隔，因此优先在换行处断开；单行本身超限时才硬切，
    保证不会把一段卡券丢空 —— 宁可发出超长被拒，也不能静默少发货。
    """
    content = str(text or "")
    if not content.strip():
        return []
    if len(content) <= limit:
        return [content]

    segments: List[str] = []
    current = ""
    for line in content.split("\n"):
        # 单行本身就超限：先冲刷已 accumulated 的内容，再对该行硬切。
        while len(line) > limit:
            if current:
                segments.append(current)
                current = ""
            segments.append(line[:limit])
            line = line[limit:]

        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) > limit:
            if current:
                segments.append(current)
            current = line
        else:
            current = candidate

    if current:
        segments.append(current)
    return [seg for seg in segments if seg.strip()]


def parse_image_payload(content: str) -> tuple[int | None, str]:
    """解析 ``__IMAGE_SEND__`` 标记，返回 ``(card_id, image_url)``。

    卡券 ID 只用于日志与图片重传归属，解析失败不影响发送，因此非法值
    降级为 ``None`` 而不是抛错。
    """
    data = content[len(IMAGE_SEND_PREFIX):]
    if "|" not in data:
        return None, data
    card_id_raw, image_url = data.split("|", 1)
    try:
        card_id = int(card_id_raw)
    except (TypeError, ValueError):
        logger.warning(f"图片发货内容包含无效卡券ID，已忽略: {card_id_raw!r}")
        card_id = None
    return card_id, image_url


async def _send_one(live_instance: Any, ws: Any, cid: str, toid: str, text: str) -> None:
    """发送一段文本。优先用同步 ``send_msg``，兼容仅有 ``send_im_text`` 的实例。"""
    sender = getattr(live_instance, "send_msg", None)
    if callable(sender):
        result = sender(ws, cid, toid, text)
    else:
        fallback = getattr(live_instance, "send_im_text", None)
        if not callable(fallback):
            raise AttributeError("账号实例缺少消息发送方法（send_msg / send_im_text）")
        result = fallback(cid, toid, text)
    if inspect.isawaitable(result):
        await result


async def send_payload(
    live_instance: Any,
    ws: Any,
    cid: str,
    toid: str,
    content: Any,
    *,
    segment_limit: int = MAX_SEGMENT_LENGTH,
    segment_interval: float = SEGMENT_INTERVAL_SECONDS,
) -> int:
    """发送一条发货内容，返回实际发出的消息段数。

    发送失败时抛出底层异常，由调用方计入该订单的发货错误；本函数只在
    至少发出一段后返回，因此返回值可以直接用于"是否全部发出"的判断。
    """
    text = content if isinstance(content, str) else str(content or "")
    if not text.strip():
        raise ValueError("发货内容为空，无法发送")

    if text.startswith(IMAGE_SEND_PREFIX):
        card_id, image_url = parse_image_payload(text)
        send_image = getattr(live_instance, "send_image_msg", None)
        if not callable(send_image):
            raise AttributeError("账号实例不支持图片消息发送")
        result = send_image(ws, cid, toid, image_url, card_id=card_id)
        if inspect.isawaitable(result):
            await result
        return 1

    segments = split_text(text, segment_limit)
    if not segments:
        raise ValueError("发货内容为空，无法发送")

    for index, segment in enumerate(segments):
        await _send_one(live_instance, ws, cid, toid, segment)
        if index < len(segments) - 1 and segment_interval > 0:
            await asyncio.sleep(segment_interval)
    return len(segments)


__all__ = [
    "IMAGE_SEND_PREFIX",
    "MAX_SEGMENT_LENGTH",
    "SEGMENT_INTERVAL_SECONDS",
    "parse_image_payload",
    "send_payload",
    "split_text",
]
