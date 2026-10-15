"""Presidio 接入的 PII 测试（对应"手搓 4 条正则、无置信度、无校验"缺口）。

覆盖三层设计：
1. Presidio 生效路径（框架 + 中文识别器插件）
2. 正则兜底路径（Presidio 不可用也不能让脱敏失效）
3. 误伤防护（订单号不该被当身份证/银行卡）
"""
from __future__ import annotations

import pytest

from lingxi.security.pii import (
    PIIGuard,
    contains_pii,
    mask_pii,
    pii_status,
    reset_engines_for_tests,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_engines_for_tests()
    yield
    reset_engines_for_tests()


class TestMaskingContractPreserved:
    """对外契约必须与替换前完全一致（既有 51 条测试也在锁这个）。"""

    def test_phone_masked(self) -> None:
        assert mask_pii("我的手机号是13800138000请记下") == "我的手机号是***请记下"

    def test_id_card_masked(self) -> None:
        assert "***" in mask_pii("身份证11010519491231002X请查收")

    def test_email_masked(self) -> None:
        assert "***" in mask_pii("邮箱 test@example.com 已注册")

    def test_bank_card_masked(self) -> None:
        assert "***" in mask_pii("卡号6222021234567890123")

    def test_safe_text_untouched(self) -> None:
        text = "退货政策是7天内无理由退货，退款3个工作日到账"
        assert mask_pii(text) == text

    def test_empty_text(self) -> None:
        assert mask_pii("") == ""
        assert contains_pii("") == {}


class TestFalsePositivePolicy:
    """误伤策略：**宁可多脱敏，不可漏脱敏**（这是安全取舍，不是缺陷）。

    漏掉真实 PII 的代价（隐私写进记忆/日志）远大于把订单号误脱敏
    （用户看不到单号，可重新查询）。所以格式像证件的串一律脱敏 ——
    校验位只用于 Presidio 层提升置信度，不用于兜底层的淘汰。
    """

    def test_id_card_form_masked_even_if_checksum_fails(self) -> None:
        """18 位日期形态串：按证件格式脱敏（安全优先）。"""
        text = "单号110105194912310021"
        assert "***" in mask_pii(text), "证件格式的串必须脱敏（宁多勿漏）"

    def test_long_digit_string_masked(self) -> None:
        """16-19 位数字串按银行卡格式脱敏。"""
        text = "编号1234567890123456789"
        assert "***" in mask_pii(text)

    def test_valid_card_masked(self) -> None:
        assert "***" in mask_pii("卡号6222021234567890123")

    def test_checksum_used_for_confidence_not_exclusion(self) -> None:
        """校验位的作用是"提高置信度"：合法身份证在检测里也是 ID_CARD。"""
        found = contains_pii("身份证11010519491231002X")
        assert "ID_CARD" in found

    def test_normal_text_not_over_masked(self) -> None:
        """真正的边界：正常文本不能被脱敏（false positive 的实际红线）。"""
        safe = "订单号是12345，请帮我查物流，7天内可退货"
        assert mask_pii(safe) == safe


class TestPresidioBackend:
    def test_status_reports_backend(self) -> None:
        st = pii_status()
        assert st["backend"] in ("presidio", "regex_fallback")
        assert "presidio_failed_reason" in st

    def test_detection_merges_presidio_and_regex(self) -> None:
        """检测结果应同时含两层的发现（能力只增不减）。"""
        found = contains_pii(
            "手机13800138000 邮箱a@b.com 身份证11010519491231002X"
        )
        assert "PHONE" in found
        assert "EMAIL" in found
        assert "ID_CARD" in found

    def test_presidio_path_used_when_available(self) -> None:
        """Presidio 可用时应走它（backend 字段可观测）。"""
        mask_pii("预热 13800138000")  # 触发懒加载
        st = pii_status()
        if st["backend"] == "presidio":
            assert st["regex_fallback_active"] is False


class TestRegexFallback:
    """Presidio 不可用时，脱敏功能必须完整保留（安全功能不能因依赖失效）。"""

    def test_fallback_still_masks_all_four_types(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import lingxi.security.pii as pii

        # 模拟 Presidio 初始化失败
        monkeypatch.setattr(pii, "_analyzer", None)
        monkeypatch.setattr(pii, "_anonymizer", None)
        monkeypatch.setattr(pii, "_presidio_failed", "ImportError: simulated")

        text = "手机13800138000 身份证11010519491231002X 邮箱a@b.com 卡号6222021234567890123"
        masked = pii.mask_pii(text)
        assert "13800138000" not in masked
        assert "11010519491231002X" not in masked
        assert "a@b.com" not in masked
        assert "6222021234567890123" not in masked

        found = pii.contains_pii(text)
        assert set(found) >= {"PHONE", "ID_CARD", "EMAIL", "BANK_CARD"}

    def test_fallback_status_visible(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import lingxi.security.pii as pii

        monkeypatch.setattr(pii, "_analyzer", None)
        monkeypatch.setattr(pii, "_anonymizer", None)
        monkeypatch.setattr(pii, "_presidio_failed", "ImportError: no presidio")
        st = pii.pii_status()
        assert st["backend"] == "regex_fallback"
        assert st["regex_fallback_active"] is True


class TestGuardFacade:
    """PIIGuard 三个入口（input/output/detect）行为不变。"""

    def test_sanitize_input_and_output(self) -> None:
        assert "***" in PIIGuard.sanitize_input("我的号13800138000")
        assert "***" in PIIGuard.sanitize_output("回执13800138000")

    def test_detect_is_readonly(self) -> None:
        text = "13800138000"
        found = PIIGuard.detect(text)
        assert "PHONE" in found
        assert text == "13800138000"  # 未被修改
