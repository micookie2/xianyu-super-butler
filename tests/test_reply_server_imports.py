"""防止"引用了不存在的模块"这类启动期崩溃再次进入主干。

2026-09-13 的提交 39184ba 给 app.reply_server 加了三处 import，但对应文件
从未进入仓库任何分支/标签，:latest 镜像因此整条 API 线程 ModuleNotFoundError，
Web 端口起不来而主进程还"看起来在跑"。语法检查和常规单测都发现不了它 ——
只有真正 import 一次才行。

这里做两件事：
1. 静态检查 reply_server 里所有 ``app.*`` 导入是否指向磁盘上的真实文件；
2. 实际 import 被引用的模块，确认可导入且导出名字与引用处一致。
"""

import ast
import importlib
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPLY_SERVER = PROJECT_ROOT / "app" / "reply_server.py"


def _app_imports(path: Path):
    """返回 (模块名, 引用的符号列表)；符号列表在 from X import a, b 时非空。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
            imports.append((node.module, [alias.name for alias in node.names]))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app."):
                    imports.append((alias.name, []))
    return imports


class AppImportsExistTest(unittest.TestCase):
    def test_reply_server_has_app_imports(self):
        """自检：解析器确实抓到了 app.* 导入，避免测试静默空跑。"""
        self.assertGreater(len(_app_imports(REPLY_SERVER)), 10)

    def test_every_app_module_resolves_to_a_file(self):
        missing = []
        for module, symbols in _app_imports(REPLY_SERVER):
            relative = Path(*module.split("."))
            if not (PROJECT_ROOT / f"{relative}.py").exists() and not (PROJECT_ROOT / relative / "__init__.py").exists():
                missing.append(module)
        self.assertEqual(
            missing,
            [],
            "app.reply_server 引用了不存在的模块，容器会以 ModuleNotFoundError 起不来："
            + ", ".join(sorted(set(missing))),
        )

    def test_referenced_symbols_are_importable(self):
        for module, symbols in _app_imports(REPLY_SERVER):
            if not symbols:
                continue
            loaded = importlib.import_module(module)
            for symbol in symbols:
                if symbol == "*":
                    continue
                with self.subTest(module=module, symbol=symbol):
                    self.assertTrue(
                        hasattr(loaded, symbol),
                        f"{module} 缺少 reply_server 需要的 {symbol}",
                    )


class DeliveryTemplateTest(unittest.TestCase):
    def test_split_preserves_every_card_and_respects_limit(self):
        from app.delivery_template import MAX_SEGMENT_LENGTH, split_text

        lines = [f"CODE-{i}-{'x' * 30}" for i in range(200)]
        segments = split_text("\n".join(lines))
        self.assertTrue(all(len(seg) <= MAX_SEGMENT_LENGTH for seg in segments))
        self.assertGreater(len(segments), 1)
        self.assertEqual("\n".join(segments).split("\n"), lines)

    def test_short_and_blank(self):
        from app.delivery_template import split_text

        self.assertEqual(split_text("一条卡券"), ["一条卡券"])
        self.assertEqual(split_text("   \n "), [])

    def test_single_line_over_limit_is_hard_cut(self):
        from app.delivery_template import split_text

        text = "一" * 5000
        segments = split_text(text)
        self.assertEqual("".join(segments), text)
        self.assertTrue(all(len(seg) <= 2000 for seg in segments))

    def test_image_marker_parsing(self):
        from app.delivery_template import parse_image_payload

        self.assertEqual(parse_image_payload("__IMAGE_SEND__12|http://a"), (12, "http://a"))
        self.assertEqual(parse_image_payload("__IMAGE_SEND__http://a"), (None, "http://a"))
        self.assertEqual(parse_image_payload("__IMAGE_SEND__abc|http://a"), (None, "http://a"))

    def test_send_payload_returns_segment_count(self):
        import asyncio

        from app.delivery_template import send_payload

        class Live:
            def __init__(self):
                self.texts = []
                self.images = []

            async def send_msg(self, ws, cid, toid, text):
                self.texts.append(text)

            async def send_image_msg(self, ws, cid, toid, url, card_id=None):
                self.images.append((url, card_id))

        live = Live()
        count = asyncio.run(
            send_payload(live, None, "c", "b", "\n".join(f"K{i}" for i in range(3)), segment_interval=0)
        )
        self.assertEqual(count, 1)
        self.assertEqual(len(live.images), 0)

        image_only = Live()
        self.assertEqual(
            asyncio.run(send_payload(image_only, None, "c", "b", "__IMAGE_SEND__7|http://a")),
            1,
        )
        self.assertEqual(image_only.images, [("http://a", 7)])

        with self.assertRaises(ValueError):
            asyncio.run(send_payload(Live(), None, "c", "b", "  "))


class NotificationTestServiceTest(unittest.TestCase):
    @staticmethod
    def _rule(**overrides):
        rule = {
            "id": 7,
            "cookie_id": "ck1",
            "channel_id": 3,
            "name": "发货通知",
            "enabled": True,
            "channel_name": "钉钉群",
            "channel_type": "dingtalk",
            "channel_config": '{"webhook_url": "https://example.com/hook"}',
        }
        rule.update(overrides)
        return rule

    @staticmethod
    def _db(rule):
        class DB:
            def get_notification_test_target(self, rule_id, user_id):
                return rule

        return DB()

    def test_success_matches_frontend_contract(self):
        import asyncio

        from app.services.notification_test import NotificationTestRateLimiter, NotificationTestService

        class Sender:
            def __init__(self):
                self.message = None

            async def send(self, channel_type, config, message, request_id=None):
                self.message = message

                class Receipt:
                    status_code = 200

                return Receipt()

        sender = Sender()
        service = NotificationTestService(
            self._db(self._rule()), sender=sender, limiter=NotificationTestRateLimiter()
        )
        result = asyncio.run(service.send_rule_test(7, 1, {"user_id": 1}))
        for key in ("success", "message", "request_id", "sent_at", "channel", "duration_ms"):
            self.assertIn(key, result)
        self.assertEqual(result["channel"], {"id": 3, "name": "钉钉群", "type": "dingtalk"})
        self.assertIn("通知测试", sender.message)

    def test_missing_rule_is_404(self):
        import asyncio

        from app.services.notification_test import NotificationTestError, NotificationTestService

        with self.assertRaises(NotificationTestError) as ctx:
            asyncio.run(NotificationTestService(self._db(None)).send_rule_test(9, 1))
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("message", ctx.exception.detail())

    def test_invalid_channel_config_is_400(self):
        import asyncio

        from app.services.notification_test import NotificationTestError, NotificationTestService

        rule = self._rule(channel_config="{}")
        with self.assertRaises(NotificationTestError) as ctx:
            asyncio.run(NotificationTestService(self._db(rule)).send_rule_test(7, 1))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("webhook_url", ctx.exception.detail()["message"])

    def test_rate_limiter_blocks_bursts(self):
        from app.services.notification_test import NotificationTestError, NotificationTestRateLimiter

        limiter = NotificationTestRateLimiter(limit=2, window_seconds=60)
        limiter.check("u1")
        limiter.check("u1")
        with self.assertRaises(NotificationTestError) as ctx:
            limiter.check("u1")
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertGreater(ctx.exception.retry_after, 0)
        self.assertIsNone(limiter.check("u2"))


if __name__ == "__main__":
    unittest.main()
