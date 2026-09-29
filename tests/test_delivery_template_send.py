"""发货内容发送：图片标记与文本分段的回归测试。

上游 2c11ac2 补交 app/delivery_template.py 时，只恢复了文本分段链路，
丢了 image 类型卡券的 ``__IMAGE_SEND__`` 分支（该分支此前内联在
reply_server 的自动发货循环里）。结果 image 卡券会把内部标记原样发给买家。
这里把两条链路都钉住。
"""

import asyncio
import unittest

from app.delivery_template import (
    IMAGE_SEND_PREFIX,
    MAX_SEGMENT_CHARS,
    parse_image_marker,
    send_payload,
    split_delivery_content,
)


class _RecordingLive:
    """记录两次发送通道各自收到了什么。"""

    def __init__(self):
        self.texts = []
        self.images = []

    async def send_msg(self, ws, chat_id, buyer_id, text):
        self.texts.append(text)

    async def send_image_msg(self, ws, chat_id, buyer_id, image_url, card_id=None):
        self.images.append((image_url, card_id))


class ImageMarkerTest(unittest.TestCase):
    def test_new_format_with_card_id(self):
        self.assertEqual(
            parse_image_marker("__IMAGE_SEND__42|https://img/x.png"),
            (42, "https://img/x.png"),
        )

    def test_legacy_format_without_card_id(self):
        self.assertEqual(
            parse_image_marker("__IMAGE_SEND__https://img/x.png"),
            (None, "https://img/x.png"),
        )

    def test_invalid_card_id_degrades_instead_of_raising(self):
        # 卡券 ID 只用于归属与日志，非法值不能阻断发货。
        self.assertEqual(
            parse_image_marker("__IMAGE_SEND__abc|https://img/x.png"),
            (None, "https://img/x.png"),
        )

    def test_url_containing_pipe_keeps_the_url_intact(self):
        self.assertEqual(
            parse_image_marker("__IMAGE_SEND__7|https://img/x.png?a=1|2"),
            (7, "https://img/x.png?a=1|2"),
        )


class SendPayloadImageTest(unittest.TestCase):
    def test_image_marker_is_sent_as_image_not_as_text(self):
        """核心回归：买家不能收到 "__IMAGE_SEND__42|https://..." 这串内部标记。"""
        live = _RecordingLive()
        count = asyncio.run(
            send_payload(live, None, "chat@goofish", "buyer@goofish",
                         f"{IMAGE_SEND_PREFIX}42|https://img/x.png")
        )
        self.assertEqual(count, 1)
        self.assertEqual(live.images, [("https://img/x.png", 42)])
        self.assertEqual(live.texts, [])

    def test_legacy_image_marker(self):
        live = _RecordingLive()
        count = asyncio.run(
            send_payload(live, None, "c", "b", f"{IMAGE_SEND_PREFIX}https://img/y.png")
        )
        self.assertEqual(count, 1)
        self.assertEqual(live.images, [("https://img/y.png", None)])
        self.assertEqual(live.texts, [])


class SendPayloadTextTest(unittest.TestCase):
    def test_short_text_goes_out_as_one_message(self):
        live = _RecordingLive()
        count = asyncio.run(send_payload(live, None, "c", "b", "卡密：ABCD-1234"))
        self.assertEqual(count, 1)
        self.assertEqual(live.texts, ["卡密：ABCD-1234"])
        self.assertEqual(live.images, [])

    def test_long_text_is_split_and_keeps_every_card(self):
        lines = [f"CODE-{i}-{'x' * 20}" for i in range(200)]
        live = _RecordingLive()
        count = asyncio.run(send_payload(live, None, "c", "b", "\n".join(lines)))
        self.assertGreater(count, 1)
        self.assertEqual(count, len(live.texts))
        # 分段只允许在换行处断开：拼回去必须与原文逐行一致，一张卡都不能丢
        self.assertEqual("\n".join(live.texts).split("\n"), lines)

    def test_blank_content_sends_nothing(self):
        live = _RecordingLive()
        self.assertEqual(asyncio.run(send_payload(live, None, "c", "b", "   ")), 0)
        self.assertEqual((live.texts, live.images), ([], []))

    def test_split_respects_limit_on_single_line(self):
        text = "一" * (MAX_SEGMENT_CHARS * 2 + 7)
        segments = split_delivery_content(text)
        self.assertTrue(all(len(seg) <= MAX_SEGMENT_CHARS for seg in segments))
        self.assertEqual("".join(segments), text)


if __name__ == "__main__":
    unittest.main()
