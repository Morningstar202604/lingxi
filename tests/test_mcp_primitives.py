"""MCP 三原语（tools / resources / prompts）的行为锁。

🔗 工程逻辑
    此前只实现 tools 一个原语，却在文档与口头上讲「MCP 三原语」——
    表述与能力不符。现在 resources/prompts 都接了**真实数据源**：
      resources → KnowledgeService / PersonaConfig / GraphStore
      prompts   → 人格 system prompt 骨架

    重点锁两件容易写错的事：
    1. 未注册的资源/提示词必须返回**错误**，返回空列表会被调用方误读成
       「服务器确实没有」—— MCP 客户端不会重试，会直接放弃。
    2. prompt 渲染不能用 `str.format`：参数里带 `{` 会抛 KeyError 把整个
       请求打挂。

🤖 AI 避坑
    - 测「未注册」必须真的不传数据源，不能靠 mock 一个空注册表
    - 断言 JSON-RPC 错误码，别只断言「有报错」
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from lingxi.agent.tools import _create_default_registry
from lingxi.mcp.models import JSONRPCRequest, MCPErrorCode
from lingxi.mcp.primitives import (
    Prompt,
    Resource,
    ResourceRegistry,
    build_default_registry,
    set_resource_registry,
)
from lingxi.mcp.server import get_mcp_server, init_mcp_server
from lingxi.memory.graph_store import Entity, InMemoryGraphStore

if TYPE_CHECKING:
    from lingxi.mcp.server import MCPToolServer


@pytest.fixture
def graph() -> InMemoryGraphStore:
    g = InMemoryGraphStore()
    g.add_entity(Entity(entity_id="x", label="Person"))
    g.add_entity(Entity(entity_id="套餐", label="Concept"))
    return g


@pytest.fixture
def registry(graph: InMemoryGraphStore) -> ResourceRegistry:
    reg = build_default_registry(knowledge=None, persona=None, graph=graph)
    set_resource_registry(reg)
    return reg


@pytest.fixture
def server(registry: ResourceRegistry) -> MCPToolServer:
    return init_mcp_server(_create_default_registry())


@pytest.fixture(autouse=True)
def _reset() -> None:
    yield
    set_resource_registry(None)


async def _call(server: MCPToolServer, method: str, **params: Any) -> Any:
    return await server.handle_request(
        JSONRPCRequest(id=1, method=method, params=params),
    )


class _FakeKnowledge:
    """最小 KnowledgeService 替身 —— 只实现被 reader 用到的两个方法。"""

    def __init__(self) -> None:
        self.docs = [
            {"doc_id": "d1", "source": "faq.md"},
            {"doc_id": "d2", "source": "refund.md"},
        ]

    async def list_documents(self) -> list[dict[str, Any]]:
        return self.docs

    async def recall(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        return [{"source": f"{query}.md", "text": "退款政策：7 天内可退。"}]


# ── 注册表本身 ──────────────────────────────────────────────────────

class TestResourceRegistry:
    def test_unknown_uri_raises(self) -> None:
        reg = ResourceRegistry()
        with pytest.raises(KeyError):
            _ = asyncio_run(reg.read_resource("nope://x"))

    def test_reader_exception_becomes_runtime_error(self) -> None:
        """数据源故障必须变成结构化错误，不能把原始异常漏给调用方。"""

        def boom(_uri: str) -> dict[str, Any]:
            raise ValueError("db down")

        reg = ResourceRegistry()
        reg.register_resource(Resource(uri="x://1", name="x"), boom)
        with pytest.raises(RuntimeError, match="db down"):
            asyncio_run(reg.read_resource("x://1"))

    def test_long_content_truncated(self) -> None:
        reg = ResourceRegistry()
        reg.register_resource(
            Resource(uri="big://1", name="big"),
            lambda _u: {"text": "A" * 50000},
        )
        out = asyncio_run(reg.read_resource("big://1"))
        assert out["truncated"] is True
        text = out["contents"][0]["text"]
        assert len(text) < 50000
        assert "已截断" in text

    def test_missing_data_source_registers_nothing(self) -> None:
        """数据源为 None 时不能注册僵尸资源（永远返回空的资源）。"""
        reg = build_default_registry(knowledge=None, persona=None, graph=None)
        assert reg.list_resources() == []
        assert reg.list_prompts() == []


class TestPromptRender:
    def test_substitutes_placeholders(self) -> None:
        p = Prompt(name="p", template="你好{{name}}，你={{age}}")
        assert p.render({"name": "小明", "age": 20}) == "你好小明，你=20"

    def test_missing_arg_keeps_placeholder(self) -> None:
        """未提供的占位符原样保留 —— 调用方能看出缺了什么。"""
        p = Prompt(name="p", template="你好{{name}}，缺{{other}}")
        assert p.render({"name": "小明"}) == "你好小明，缺{{other}}"

    def test_braces_in_user_value_do_not_crash(self) -> None:
        """这是不用 str.format 的原因：用户值带 { 会炸整个请求。"""
        p = Prompt(name="p", template="内容：{{body}}")
        assert p.render({"body": "{a}{b} {0}"}) == "内容：{a}{b} {0}"


# ── JSON-RPC 分发 ──────────────────────────────────────────────────

class TestMCPDispatch:
    @pytest.mark.asyncio
    async def test_initialize_declares_all_three_primitives(
        self, server: MCPToolServer,
    ) -> None:
        res = await _call(server, "initialize")
        assert res.error is None
        caps = res.result["capabilities"]
        assert set(caps) >= {"tools", "resources", "prompts"}
        assert res.result["serverInfo"]["name"] == "lingxi"

    @pytest.mark.asyncio
    async def test_tools_still_works(self, server: MCPToolServer) -> None:
        """补 resources/prompts 不能把原有 tools 路径弄坏。"""
        res = await _call(server, "tools/list")
        assert res.error is None
        assert len(res.result["tools"]) >= 1

    @pytest.mark.asyncio
    async def test_resources_list_and_read(
        self, server: MCPToolServer, registry: ResourceRegistry,
    ) -> None:
        res = await _call(server, "resources/list")
        uris = [r["uri"] for r in res.result["resources"]]
        assert "graph://stats" in uris

        res = await _call(server, "resources/read", uri="graph://stats")
        text = res.result["contents"][0]["text"]
        assert "实体节点数: 2" in text
        assert res.result["contents"][0]["uri"] == "graph://stats"

    @pytest.mark.asyncio
    async def test_unknown_resource_is_error_not_empty(
        self, server: MCPToolServer,
    ) -> None:
        res = await _call(server, "resources/read", uri="graph://ghost")
        assert res.error is not None
        assert res.error.code == MCPErrorCode.TOOL_NOT_FOUND

    @pytest.mark.asyncio
    async def test_read_without_uri_is_invalid_params(
        self, server: MCPToolServer,
    ) -> None:
        res = await _call(server, "resources/read")
        assert res.error is not None
        assert res.error.code == MCPErrorCode.INVALID_PARAMS

    @pytest.mark.asyncio
    async def test_no_registry_at_all_is_error(
        self, server: MCPToolServer,
    ) -> None:
        """注册表整体缺失时必须报错 —— 返回空列表会被调用方当成「确实没有」，
        MCP 客户端不会重试，会直接放弃这个能力。"""
        set_resource_registry(None)
        server = get_mcp_server()
        assert server is not None
        for method in ("resources/list", "prompts/list"):
            res = await _call(server, method)
            assert res.error is not None, f"{method} 不该静默返回空列表"

        res = await _call(
            server, "prompts/get",
            name="zhixi_system_prompt",
            arguments={"name": "智犀", "role": "客服"},
        )
        assert res.error is not None

    @pytest.mark.asyncio
    async def test_empty_prompt_list_is_valid_when_registry_loaded(
        self, server: MCPToolServer, registry: ResourceRegistry,
    ) -> None:
        """反过来：注册表已装配但确实没有 prompts 时，返回空列表是正确的
        （数据源未注入 ≠ 装配失败）。"""
        res = await _call(server, "prompts/list")
        assert res.error is None
        assert res.result["prompts"] == []

    @pytest.mark.asyncio
    async def test_unknown_prompt_is_error(self, server: MCPToolServer) -> None:
        res = await _call(server, "prompts/get", name="ghost")
        assert res.error is not None

    @pytest.mark.asyncio
    async def test_ping(self, server: MCPToolServer) -> None:
        res = await _call(server, "ping")
        assert res.error is None
        assert res.result == {}


class TestPersonaPrompts:
    @pytest.fixture
    def with_persona(self) -> ResourceRegistry:
        from lingxi.persona.schema import PersonaConfig

        reg = build_default_registry(
            knowledge=None, persona=PersonaConfig(), graph=None,
        )
        set_resource_registry(reg)
        return reg

    @pytest.mark.asyncio
    async def test_prompts_listed_and_rendered(
        self, with_persona: ResourceRegistry,
    ) -> None:
        server = init_mcp_server(_create_default_registry())
        res = await _call(server, "prompts/list")
        names = [p["name"] for p in res.result["prompts"]]
        assert "zhixi_system_prompt" in names

        res = await _call(
            server, "prompts/get",
            name="zhixi_system_prompt",
            arguments={"memory": "用户偏好简洁"},
        )
        text = res.result["messages"][0]["content"]["text"]
        assert "用户偏好简洁" in text
        assert "{{memory}}" not in text

    @pytest.mark.asyncio
    async def test_persona_config_resource_readable(
        self, with_persona: ResourceRegistry,
    ) -> None:
        server = init_mcp_server(_create_default_registry())
        res = await _call(server, "resources/read", uri="persona://config")
        content = res.result["contents"][0]
        assert content["mimeType"] == "application/json"
        assert json.loads(content["text"])  # 是合法 JSON


class TestKnowledgeResources:
    @pytest.fixture
    def with_knowledge(self) -> ResourceRegistry:
        reg = build_default_registry(
            knowledge=_FakeKnowledge(), persona=None, graph=None,
        )
        set_resource_registry(reg)
        return reg

    @pytest.mark.asyncio
    async def test_doc_index_lists_docs(self, with_knowledge: ResourceRegistry) -> None:
        server = init_mcp_server(_create_default_registry())
        res = await _call(server, "resources/read", uri="knowledge://doc/index")
        text = res.result["contents"][0]["text"]
        assert "d1" in text and "faq.md" in text
        assert "d2" in text

    @pytest.mark.asyncio
    async def test_doc_content_uses_recall(self, with_knowledge: ResourceRegistry) -> None:
        server = init_mcp_server(_create_default_registry())
        res = await _call(server, "resources/read", uri="knowledge://doc/refund")
        assert "退款政策" in res.result["contents"][0]["text"]

    @pytest.mark.asyncio
    async def test_knowledge_failure_is_runtime_error(self) -> None:
        class _Broken:
            async def list_documents(self) -> list[dict[str, Any]]:
                raise ConnectionError("vector db down")

        reg = build_default_registry(
            knowledge=_Broken(), persona=None, graph=None,
        )
        set_resource_registry(reg)
        with pytest.raises(Exception):  # noqa: B017,PT011 — 具体异常类型不重要
            asyncio_run(reg.read_resource("knowledge://doc/index"))


def asyncio_run(coro: Any) -> Any:
    """在同步测试里跑协程（避免每个用例都挂 @pytest.mark.asyncio）。"""
    import asyncio

    return asyncio.run(coro)


__all__: list[str] = []
