"""SKU 识别错误分类的回归测试。

``app/reply_server.py`` 两处 SKU 识别接口调用
``classify_sku_discovery_error(exc)``，但该函数从未存在于
utils/xianyu_seller_api.py（与 39184ba 丢模块是同一批遗漏）。因为 import
写在 handler 函数体内、且在 Cookie 校验之后，启动阶段完全不报错 —— 只有真的
点了"识别 SKU"才 500，比启动崩溃更难发现。

这里同时钉住调用方依赖的返回词表：``risk_control`` / ``unauthorized`` /
``error``，以及"风控优先于登录态"这条判定顺序。
"""

import unittest

from utils.risk_control import RISK_CONTROL_MARKERS
from utils.xianyu_seller_api import (
    UNAUTHORIZED_MARKERS,
    SellerApiError,
    classify_sku_discovery_error,
)


def _error(*ret: str) -> SellerApiError:
    return SellerApiError("mtop.idle.web.trade.item.sku.list", list(ret))


class ClassificationTest(unittest.TestCase):
    def test_risk_control_markers(self):
        for marker in RISK_CONTROL_MARKERS:
            with self.subTest(marker=marker):
                self.assertEqual(
                    classify_sku_discovery_error(_error(f"FAIL::{marker}")),
                    "risk_control",
                )

    def test_unauthorized_markers(self):
        for marker in UNAUTHORIZED_MARKERS:
            with self.subTest(marker=marker):
                self.assertEqual(
                    classify_sku_discovery_error(_error(f"FAIL::{marker}::详情")),
                    "unauthorized",
                )

    def test_ordinary_business_failure_is_error(self):
        self.assertEqual(
            classify_sku_discovery_error(_error("FAIL_BIZ_ITEM_NOT_FOUND::商品不存在")),
            "error",
        )

    def test_risk_control_wins_over_session_expiry(self):
        """平台限流也会返回 FAIL_SYS_ 开头的码；误判成未登录会把用户支去重新扫码。"""
        combined = _error("FAIL_SYS_USER_VALIDATE::哎哟喂，网络被挤爆", "FAIL_SYS_SESSION_EXPIRED")
        self.assertEqual(classify_sku_discovery_error(combined), "risk_control")

    def test_falls_back_to_message_when_ret_missing(self):
        # 兼容非本模块构造的异常对象：没有 ret 属性时读 str(exc)。
        class _Shim:
            ret = None

            def __str__(self):
                return "调用失败: FAIL_SYS_SESSION_EXPIRED::Session过期"

        self.assertEqual(classify_sku_discovery_error(_Shim()), "unauthorized")

    def test_empty_error_does_not_raise(self):
        self.assertEqual(classify_sku_discovery_error(_error()), "error")
        self.assertEqual(classify_sku_discovery_error(None), "error")

    def test_only_documented_statuses_are_returned(self):
        """调用方用 == 和 in 判断状态，出现第四个值会静默走到 502 分支。"""
        allowed = {"risk_control", "unauthorized", "error"}
        samples = [
            _error("SUCCESS"),
            _error("FAIL_SYS_FLOW_LIMIT::请稍后重试"),
            _error("FAIL_SYS_TOKEN_EMPTY::令牌为空"),
            _error("FAIL_BIZ_UNKNOWN"),
            _error("哎哟喂"),
            SellerApiError("api", []),
        ]
        for sample in samples:
            with self.subTest(sample=str(sample)):
                self.assertIn(classify_sku_discovery_error(sample), allowed)


if __name__ == "__main__":
    unittest.main()
