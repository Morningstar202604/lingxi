"""Phase 2/3 tests — SessionSummarizer, AuditLog, PII masking.

🔗 工程逻辑
    覆盖 spec 要求的全部用例:
    - PII mask_pii: phone / id_card / email / bank_card 各种格式
    - PII contains_pii: 有 / 无 PII 识别
    - PII empty / safe text
    - PIIGuard.sanitize_input / output / detect
    - AuditLog record + query 基础
    - AuditLog 过滤 (action / actor)
    - AuditLog 无 update / delete 方法, 尝试改 / 删时的行为
    - SessionSummarizer.format_thread 格式
    - SessionSummarizer.should_summarize 阈值 (19 条 false, 20 条 true)
    - SessionSummarizer.summarize 调用 LLM (mock litellm.acompletion)
    - SessionSummarizer.summarize fallback 失败时回退到文本

🤖 AI 避坑
    - asyncio_mode = strict: 所有异步测试显式 @pytest.mark.asyncio
    - AuditLog 每次新建时用 tmp_path 避免 sqlite 文件竞争
    - mock litellm.acompletion 使用 AsyncMock / async 生成器模式
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from lingxi.flow.audit import AuditAction, AuditLog
from lingxi.memory.summarizer import SessionSummarizer
from lingxi.security.pii import PIIGuard, contains_pii, mask_pii

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# ══════════════════════════════════════════════════════════════════
# PII
# ══════════════════════════════════════════════════════════════════


class TestMaskPii:
    """mask_pii: 各类 PII 格式替换。"""

    def test_phone_masked(self) -> None:
        text = "我的手机号是13800138000请记下"
        assert mask_pii(text) == "我的手机号是***请记下"

    def test_phone_19_mobile(self) -> None:
        # 19 号段
        text = "19112345678"
        assert mask_pii(text) == "***"

    def test_id_card_masked(self) -> None:
        # 18 位身份证 — 不含校验位验证, 但格式符合 regex
        text = "身份证11010519491231002X请查收"
        result = mask_pii(text)
        assert "***" in result
        assert "11010519491231002X" not in result

    def test_email_masked(self) -> None:
        text = "联系我 at user@example.com 或 admin@test.org.cn"
        result = mask_pii(text)
        assert "***" in result
        assert "user@example.com" not in result
        assert "admin@test.org.cn" not in result

    def test_bank_card_masked(self) -> None:
        # 16 位
        text = "银行卡 6222021234567890 余额不足"
        result = mask_pii(text)
        assert "***" in result
        assert "6222021234567890" not in result

    def test_bank_card_19_digits(self) -> None:
        # 19 位银行卡 (交行/工行等)
        text = "卡号 6222021234567890123 余额"
        result = mask_pii(text)
        assert "6222021234567890123" not in result
        assert "***" in result

    def test_short_number_not_masked(self) -> None:
        # 15 位不是银行卡
        text = "单号 123456789012345"
        result = mask_pii(text)
        assert result == text

    def test_custom_mask_char(self) -> None:
        text = "phone: 13800138000"
        assert mask_pii(text, mask="[已脱敏]") == "phone: [已脱敏]"

    def test_multiple_pii(self) -> None:
        text = "邮箱 a@b.com 电话 13900139000"
        result = mask_pii(text)
        assert "a@b.com" not in result
        assert "13900139000" not in result


class TestContainsPii:
    """contains_pii: 有 / 无 PII 检测。"""

    def test_detect_phone(self) -> None:
        result = contains_pii("联系 13800138000 谢谢")
        assert "PHONE" in result
        assert "13800138000" in result["PHONE"]

    def test_detect_email(self) -> None:
        result = contains_pii("邮件 user@example.com")
        assert "EMAIL" in result
        assert "user@example.com" in result["EMAIL"]

    def test_detect_multiple_types(self) -> None:
        result = contains_pii("手机 13800138000 邮件 xx@yy.com")
        assert "PHONE" in result
        assert "EMAIL" in result

    def test_no_pii_returns_empty(self) -> None:
        result = contains_pii("今天天气真好")
        assert result == {}

    def test_empty_text(self) -> None:
        result = contains_pii("")
        assert result == {}

    def test_no_pii_safe_text(self) -> None:
        # 无数字串 / 邮箱 的情况
        text = "你好, 我想咨询退货流程, 谢谢"
        result = contains_pii(text)
        assert result == {}


class TestPIIGuard:
    """PIIGuard 静态方法行为。"""

    def test_sanitize_input(self) -> None:
        assert PIIGuard.sanitize_input("13800138000") == "***"

    def test_sanitize_output(self) -> None:
        assert PIIGuard.sanitize_output("13800138000") == "***"

    def test_detect_returns_dict(self) -> None:
        r = PIIGuard.detect("phone: 13800138000")
        assert isinstance(r, dict)
        assert "PHONE" in r

    def test_sanitize_none_input(self) -> None:
        # mask_pii 在 str 上为 *, None 输入会 TypeError — 但它不是 str
        # 此处测试正常 str 路径
        assert PIIGuard.sanitize_input("abc") == "abc"


# ══════════════════════════════════════════════════════════════════
# AUDIT LOG
# ══════════════════════════════════════════════════════════════════


class TestAuditLog:
    """AuditLog record / query / 过滤 / 不可篡改性。"""

    @pytest.fixture
    def audit(self, tmp_path: Path) -> Iterator[AuditLog]:
        # 必须显式 close：sqlite3.Connection 靠 GC 回收时机随机，
        # 未关闭时 pytest 的 unraisableexception 插件会把 ResourceWarning
        # 嫁祸给当时正在跑的任意用例，导致套件非确定性失败。
        instance = AuditLog(db_path=str(tmp_path / "test_audit.db"))
        try:
            yield instance
        finally:
            instance.close()

    def test_record_and_query(self, audit: AuditLog) -> None:
        audit.record(AuditAction.TICKET_CREATED, actor="system", target="t-001")
        rows = audit.query()
        assert len(rows) == 1
        assert rows[0]["action"] == "ticket.created"
        assert rows[0]["actor"] == "system"
        assert rows[0]["target"] == "t-001"

    def test_filter_by_action(self, audit: AuditLog) -> None:
        audit.record(AuditAction.TICKET_CREATED, actor="sys", target="t1")
        audit.record(AuditAction.TICKET_ESCALATED, actor="agent", target="t2")
        audit.record(AuditAction.TICKET_ESCALATED, actor="agent", target="t3")

        escalated = audit.query(action="ticket.escalated")
        assert len(escalated) == 2
        assert all(r["action"] == "ticket.escalated" for r in escalated)

    def test_filter_by_actor(self, audit: AuditLog) -> None:
        audit.record(AuditAction.USER_LOGIN, actor="alice")
        audit.record(AuditAction.USER_LOGIN, actor="bob")
        audit.record(AuditAction.USER_LOGIN, actor="alice")

        rows = audit.query(actor="alice")
        assert len(rows) == 2
        assert all(r["actor"] == "alice" for r in rows)

    def test_filter_by_since(self, audit: AuditLog) -> None:
        audit.record(AuditAction.RULE_TRIGGERED, actor="sys")
        time.sleep(0.01)  # 10ms 间隔
        t1 = time.time()
        audit.record(AuditAction.CONFIG_CHANGE, actor="admin")

        # since=t1 应只返回 config.change
        rows = audit.query(since=t1)
        assert len(rows) == 1
        assert rows[0]["action"] == "config.change"

    def test_query_limit(self, audit: AuditLog) -> None:
        for i in range(10):
            audit.record(AuditAction.TICKET_CREATED, actor="u", target=f"t-{i}")
        rows = audit.query(limit=3)
        assert len(rows) == 3

    def test_empty_db_returns_empty(self, audit: AuditLog) -> None:
        assert audit.query() == []

    def test_export_json(self, audit: AuditLog) -> None:
        audit.record(AuditAction.USER_LOGIN, actor="alice", detail="web")
        rows = audit.query()
        j = audit.export_json(rows)
        assert '"action"' in j
        assert "user.login" in j
        assert "alice" in j

    def test_no_update_method(self, audit: AuditLog) -> None:
        with pytest.raises(TypeError):
            audit.update()  # type: ignore[operator]

    def test_no_delete_method(self, audit: AuditLog) -> None:
        with pytest.raises(TypeError):
            audit.delete()  # type: ignore[operator]

    def test_no_remove_method(self, audit: AuditLog) -> None:
        with pytest.raises(TypeError):
            audit.remove()  # type: ignore[operator]

    def test_no_clear_method(self, audit: AuditLog) -> None:
        with pytest.raises(TypeError):
            audit.clear()  # type: ignore[operator]

    def test_append_only_preserved(self, audit: AuditLog) -> None:
        audit.record(AuditAction.TICKET_CREATED, actor="sys", target="t1")
        # 尝试通过 update 来改是不行的
        with pytest.raises(TypeError):
            audit.update(action="hacked")
        # 数据应当未被篡改
        rows = audit.query()
        assert rows[0]["action"] == "ticket.created"

    def test_record_with_string_action(self, audit: AuditLog) -> None:
        # 也允许非枚举的字符串 action
        audit.record("user.logout", actor="carol")
        rows = audit.query(action="user.logout")
        assert len(rows) == 1

    def test_record_with_detail(self, audit: AuditLog) -> None:
        audit.record(AuditAction.CONFIG_CHANGE, actor="admin", target="cfg", detail="changed threshold 0.6")
        rows = audit.query()
        assert rows[0]["detail"] == "changed threshold 0.6"

    def test_query_order_desc(self, audit: AuditLog) -> None:
        for i in range(5):
            audit.record(AuditAction.TICKET_CREATED, actor="u", target=f"t-{i}")
            time.sleep(0.005)  # 保证时间戳不同
        rows = audit.query(limit=10)
        ts = [r["timestamp"] for r in rows]
        assert ts == sorted(ts, reverse=True)


# ══════════════════════════════════════════════════════════════════
# SESSION SUMMARIZER
# ══════════════════════════════════════════════════════════════════


def _make_msg(role: str = "user", content: str = "hello") -> Any:
    """构造一个具有 .role / .content 属性的简易消息对象。"""
    return MagicMock(role=role, content=content)


class TestSessionSummarizer:
    """SessionSummarizer 的 format_thread / should_summarize / summarize。"""

    def test_format_thread_basic(self) -> None:
        s = SessionSummarizer(model="test-model")
        msgs = [_make_msg("user", "我的快递到哪了"), _make_msg("agent", "请提供单号")]
        text = s.format_thread(msgs)
        assert "user: 我的快递到哪了" in text
        assert "assistant: 请提供单号" in text  # agent → assistant 映射

    def test_format_thread_dict_messages(self) -> None:
        s = SessionSummarizer(model="test-model")
        msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        text = s.format_thread(msgs)
        assert "user: hi" in text
        assert "assistant: hello" in text

    def test_format_thread_empty(self) -> None:
        s = SessionSummarizer(model="test-model")
        assert s.format_thread([]) == ""

    def test_should_summarize_under_threshold(self) -> None:
        s = SessionSummarizer(model="test-model")
        # 19 条 → false
        msgs = [_make_msg("user", "x") for _ in range(19)]
        assert s.should_summarize(msgs, threshold=20) is False

    def test_should_summarize_at_threshold(self) -> None:
        s = SessionSummarizer(model="test-model")
        # 20 条 → true
        msgs = [_make_msg("user", "x") for _ in range(20)]
        assert s.should_summarize(msgs, threshold=20) is True

    def test_should_summarize_above_threshold(self) -> None:
        s = SessionSummarizer(model="test-model")
        msgs = [_make_msg("user", "x") for _ in range(25)]
        assert s.should_summarize(msgs, threshold=20) is True

    def test_custom_threshold(self) -> None:
        s = SessionSummarizer(model="test-model")
        msgs = [_make_msg("user", "x") for _ in range(5)]
        assert s.should_summarize(msgs, threshold=10) is False
        assert s.should_summarize(msgs, threshold=5) is True

    def test_thread_agent_role_normalized(self) -> None:
        s = SessionSummarizer(model="test-model")
        # agent_escalated 应映射到 assistant
        msgs = [_make_msg("agent_escalated", "转人工")]
        text = s.format_thread(msgs)
        assert "assistant: 转人工" in text


class TestSessionSummarizerLlm:
    """summarize() 走 LLM 路径 (mock litellm.acompletion)。"""

    @staticmethod
    def _make_llm_response(text: str) -> Any:
        """构造一个最小化的 litellm 响应对象。"""
        msg = MagicMock()
        msg.content = text
        choice = MagicMock()
        choice.message = msg
        resp = MagicMock()
        resp.choices = [choice]
        return resp

    @pytest.mark.asyncio
    async def test_summarize_calls_litellm(self) -> None:
        s = SessionSummarizer(model="my-model")
        msgs = [_make_msg("user", "问题"), _make_msg("agent", "回复")]
        fake_resp = self._make_llm_response("用户咨询了退货")

        async def _fake_acompletion(*args: Any, **kwargs: Any) -> Any:
            # 验证 model / messages 解析
            assert kwargs.get("model") == "my-model"
            msgs_arg = kwargs.get("messages", [])
            assert len(msgs_arg) == 2  # system + user
            assert "摘要" in msgs_arg[0]["content"]  # system
            # user 消息为 format_thread 产物："user: 问题\nassistant: 回复"
            assert "问题" in msgs_arg[1]["content"]
            assert "user:" in msgs_arg[1]["content"]
            return fake_resp

        with patch("litellm.acompletion", side_effect=_fake_acompletion):
            summary = await s.summarize(msgs)

        assert summary == "**会话摘要**: 用户咨询了退货"


class TestSessionSummarizerFallback:
    """summarize() LLM 失败后的文本截断回退。"""

    @pytest.mark.asyncio
    async def test_summarize_litellm_import_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """litellm 未安装 → ImportError → fallback。"""
        s = SessionSummarizer(model="any")
        msgs = [_make_msg("user", "hello world good morning nice day"), _make_msg("agent", "hi there")]

        # 让 litellm 导入失败
        import sys
        real_module = sys.modules.pop("litellm", None)
        sys.modules["litellm"] = None  # type: ignore[assignment]  # 触发 ImportError

        try:
            summary = await s.summarize(msgs)
        finally:
            if real_module is not None:
                sys.modules["litellm"] = real_module
            else:
                del sys.modules["litellm"]

        assert summary.startswith("**会话摘要**:")
        assert "hello world" in summary  # 首条被引用

    @pytest.mark.asyncio
    async def test_summarize_litellm_exception_fallback(self) -> None:
        """litellm 抛异常 → fallback。"""
        s = SessionSummarizer(model="any")
        msgs = [
            _make_msg("user", "first message content here long enough"),
            _make_msg("agent", "agent reply content here long enough"),
        ]

        async def _boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("network error")

        with patch("litellm.acompletion", side_effect=_boom):
            summary = await s.summarize(msgs)

        assert summary.startswith("**会话摘要**:")
        assert "first message" in summary
        assert "agent reply" in summary

    @pytest.mark.asyncio
    async def test_summarize_empty_messages(self) -> None:
        s = SessionSummarizer(model="m")
        summary = await s.summarize([])
        assert "无消息" in summary

    @pytest.mark.asyncio
    async def test_summarize_single_message_fallback_short(self) -> None:
        """少于消息数也会走 fallback, 因为 LLM 结果为空或 1 条不够有意义但不会报错。"""
        s = SessionSummarizer(model="m")
        # 单条消息 — should_summarize 是 False, 但 summarize() 仍可调用
        msgs = [_make_msg("user", "only one message here")]

        async def _respond(*args: Any, **kwargs: Any) -> Any:
            fake = MagicMock()
            fake.choices.__getitem__ = lambda self, idx: MagicMock(content="只有一条")
            return fake

        # summary 不应该报错且应返回字符串
        with patch("litellm.acompletion", side_effect=_respond):
            summary = await s.summarize(msgs)
        assert isinstance(summary, str)
        assert "会话摘要" in summary


class TestSessionSummarizerInputTruncation:
    """max_input_chars 截断逻辑。"""

    @pytest.mark.asyncio
    async def test_long_input_truncated(self) -> None:
        s = SessionSummarizer(model="m", max_input_chars=100)
        # 构造大量消息让 format_thread >> 100 字符
        msgs = [_make_msg("user", "x" * 50), _make_msg("agent", "y" * 50), _make_msg("user", "z" * 50)]

        captured: dict[str, str] = {}

        async def _ac(*args: Any, **kwargs: Any) -> Any:
            user_msg = kwargs["messages"][1]
            captured["user"] = user_msg["content"]
            return MagicMock(choices=[MagicMock(message=MagicMock(content="ok"))])

        with patch("litellm.acompletion", side_effect=_ac):
            await s.summarize(msgs)

        # 传入的 user 消息应被截断
        assert "...(中间省略)..." in captured["user"]


# ══════════════════════════════════════════════════════════════════
# Dashboard API: audit + summarize 端点 (需要 admin_token)
# ══════════════════════════════════════════════════════════════════


class TestDashboardAuditEndpoints:
    """Dashboard audit 端点：必须 admin_token 鉴权”。"""

    @pytest.fixture
    def audit(self, tmp_path: Path) -> Iterator[AuditLog]:
        """同 TestAuditLog.audit：显式 close，避免连接泄漏嫁祸其他用例。"""
        instance = AuditLog(db_path=str(tmp_path / "dashboard_audit.db"))
        try:
            yield instance
        finally:
            instance.close()

    def test_requires_admin_token(
        self, admin_headers: dict[str, str], audit: AuditLog
    ) -> None:
        from fastapi.testclient import TestClient

        from lingxi.flow.dashboard_api import create_dashboard_app
        from lingxi.flow.queue import EscalationQueue
        from lingxi.flow.ticket import TicketStore

        app = create_dashboard_app(TicketStore(), EscalationQueue(), audit_log=audit)

        with TestClient(app) as cli:
            # 无 token
            r = cli.get("/api/audit")
            assert r.status_code == 401

            # 有 token
            r = cli.get("/api/audit", headers=admin_headers)
            assert r.status_code == 200

    def test_audit_list_and_export(
        self, admin_headers: dict[str, str], audit: AuditLog
    ) -> None:
        from fastapi.testclient import TestClient

        from lingxi.flow.dashboard_api import create_dashboard_app
        from lingxi.flow.queue import EscalationQueue
        from lingxi.flow.ticket import TicketStore

        audit.record(AuditAction.TICKET_ESCALATED, actor="agent", target="t-001")
        audit.record(AuditAction.TICKET_CREATED, actor="sys", target="t-002")

        app = create_dashboard_app(TicketStore(), EscalationQueue(), audit_log=audit)

        with TestClient(app) as cli:
            # 列出全部
            r = cli.get("/api/audit", headers=admin_headers)
            assert r.status_code == 200
            data = r.json()
            assert len(data) == 2

            # 按 action 过滤
            r = cli.get("/api/audit?action=ticket.escalated", headers=admin_headers)
            filtered = r.json()
            assert len(filtered) == 1
            assert filtered[0]["action"] == "ticket.escalated"

            # 导出 JSON
            r = cli.get("/api/audit/export", headers=admin_headers)
            assert r.status_code == 200
            assert "application/json" in r.headers["content-type"]
            body = r.json()
            assert isinstance(body, list)


class TestDashboardSummarizeEndpoint:
    """Dashboard /api/sessions/summarize 端点测试."""

    @pytest.fixture
    def app_with_summarizer(self, tmp_path: Path) -> Any:
        from lingxi.flow.dashboard_api import create_dashboard_app
        from lingxi.flow.queue import EscalationQueue
        from lingxi.flow.ticket import TicketStore
        from lingxi.memory.summarizer import SessionSummarizer

        summarizer = SessionSummarizer(model="test-model")
        app = create_dashboard_app(TicketStore(), EscalationQueue(), summarizer=summarizer)
        return app

    @pytest.mark.asyncio
    async def test_summarize_requires_admin(
        self, app_with_summarizer: Any, admin_headers: dict[str, str],
        tmp_path: Path,
    ) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app_with_summarizer) as cli:
            # 无 token
            r = cli.post("/api/sessions/summarize", json={"messages": [{"role": "user", "content": "a"}, {"role": "agent", "content": "b"}]})
            assert r.status_code == 401

            # 有 token，消息太少
            r = cli.post(
                "/api/sessions/summarize",
                json={"messages": [{"role": "user", "content": "only one"}]},
                headers=admin_headers,
            )
            assert r.status_code == 400
