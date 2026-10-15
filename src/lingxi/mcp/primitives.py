"""MCP 原语注册表 — tools 之外的 resources 与 prompts。

🔗 工程逻辑
    MCP 定义三个原语：
      - **tools**   —— 模型可调用的动作（已实现，见 MCPToolServer）
      - **resources** —— 可被读取的上下文（知识库文档、实体图谱快照）
      - **prompts**  —— 可复用的提示词模板（人格 system prompt 骨架）

    此前只实现 tools 一个原语，因此「MCP 三原语」的表述是不准确的。
    这里补上另外两个，并且**接的是真实数据源**而不是硬编码占位：
      resources → KnowledgeService（已挂载的知识库）+ 图谱统计
      prompts   → PersonaConfig（运行时真实生效的人格配置）

🤖 AI 避坑
    - resource 内容要能分页/限量返回：整库塞进一个 JSON-RPC 响应会撑爆上下文
    - prompt 用参数做模板替换，别直接拼字符串（注入面）
    - 数据源不可用时返回**结构化错误**而不是空列表 —— 空列表会被
      调用方误读成"确实没有数据"
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lingxi.core import get_logger

log = get_logger(__name__)

MAX_RESOURCE_CHARS = 8000


@dataclass
class Resource:
    """一个可读取的上下文资源。"""

    uri: str
    name: str
    description: str = ""
    mime_type: str = "text/plain"

    def to_mcp_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "name": self.name,
            "description": self.description,
            "mimeType": self.mime_type,
        }


@dataclass
class Prompt:
    """一个可复用的提示词模板。"""

    name: str
    description: str = ""
    arguments: list[dict[str, Any]] = field(default_factory=list)
    template: str = ""

    def to_mcp_dict(self) -> dict[str, Any]:
        """prompts/list 的声明形状（name/description/arguments）。"""
        return {
            "name": self.name,
            "description": self.description,
            "arguments": self.arguments,
        }

    def to_get_result(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """prompts/get 的结果形状（MCP 规范要求 messages 数组）。

        与 to_mcp_dict 的区别：list 返回**声明**，get 返回**渲染后的内容**。
        此前 get 复用了声明形状，客户端拿不到 messages（实测 KeyError）。
        """
        return {
            "description": self.description,
            "messages": [
                {
                    "role": "user",
                    "content": {"type": "text", "text": self.render(args or {})},
                }
            ],
        }

    def render(self, args: dict[str, Any]) -> str:
        """参数化渲染。

        刻意**不做** str.format：用户可控参数里带 `{` 会让 format 抛
        KeyError/IndexError 把整个请求打挂。这里用显式占位符替换，
        未提供的占位符原样保留（调用方能看出缺了什么）。
        """
        out = self.template
        for key, value in args.items():
            token = "{{" + str(key) + "}}"
            if token in out:
                out = out.replace(token, str(value))
        return out


class ResourceRegistry:
    """资源与提示词注册表。

    数据源用 provider 回调注入，缺失时对应资源**不注册**
    （而不是注册一个永远返回空的僵尸资源）。
    """

    def __init__(self) -> None:
        self._resources: dict[str, Resource] = {}
        self._readers: dict[str, Any] = {}
        self._prompts: dict[str, Prompt] = {}

    # ── 注册 ──
    def register_resource(
        self,
        resource: Resource,
        reader: Any,
    ) -> None:
        """reader 是 `async def(uri) -> dict` 或 `def(uri) -> dict`。

        `resource.uri` 可以是**模板**（含 `{name}` 段），此时 reader 会被
        所有匹配该前缀的具体 URI 复用 —— 否则 `knowledge://doc/{doc_id}`
        这种声明式资源永远读不到，因为调用方传的是 `knowledge://doc/refund`。
        """
        self._resources[resource.uri] = resource
        self._readers[resource.uri] = reader

    def register_prompt(self, prompt: Prompt) -> None:
        self._prompts[prompt.name] = prompt

    # ── 查询 ──
    def list_resources(self) -> list[dict[str, Any]]:
        return [r.to_mcp_dict() for r in self._resources.values()]

    def list_prompts(self) -> list[dict[str, Any]]:
        return [p.to_mcp_dict() for p in self._prompts.values()]

    def has_resource(self, uri: str) -> bool:
        return self._resolve(uri) is not None

    def get_prompt(self, name: str) -> Prompt | None:
        return self._prompts.get(name)

    def _resolve(self, uri: str) -> tuple[Resource, Any] | None:
        """把具体 URI 解析到 (资源声明, reader)。

        先精确匹配，再退到模板匹配：`knowledge://doc/{doc_id}` 只替换
        最后一段，其余段必须逐字相等 —— 否则 `evil://doc/x` 也能命中
        `knowledge://doc/{doc_id}` 的前缀。
        """
        exact = self._resources.get(uri)
        if exact is not None:
            reader = self._readers.get(uri)
            return (exact, reader) if reader is not None else None

        for template, resource in self._resources.items():
            if "{" not in template:
                continue
            prefix, _, suffix = template.partition("{")
            name_seg, _, tail = suffix.partition("}")
            if not name_seg or "{" in tail:
                continue
            if uri.startswith(prefix) and uri.endswith(tail) and \
                    len(uri) >= len(prefix) + len(tail):
                reader = self._readers.get(template)
                return (resource, reader) if reader is not None else None
        return None

    async def read_resource(self, uri: str) -> dict[str, Any]:
        """读取资源。数据源缺失时抛 KeyError，调用方转成 JSON-RPC 错误。"""
        resolved = self._resolve(uri)
        if resolved is None:
            raise KeyError(uri)
        resource, reader = resolved

        try:
            result = reader(uri)
            if hasattr(result, "__await__"):
                result = await result
        except Exception as e:  # noqa: BLE001 — 数据源故障要转成结构化错误
            log.warning("resource_read_failed", uri=uri, error=str(e))
            raise RuntimeError(f"读取资源 '{uri}' 失败: {e}") from e

        if not isinstance(result, dict):
            result = {"text": str(result)}

        text = str(result.get("text", ""))
        if len(text) > MAX_RESOURCE_CHARS:
            text = (
                text[: MAX_RESOURCE_CHARS // 2]
                + f"\n…（内容过长已截断，共 {len(text)} 字符）…\n"
                + text[-(MAX_RESOURCE_CHARS // 2):]
            )
            result["truncated"] = True

        result.setdefault("mimeType", resource.mime_type)
        return {
            "contents": [
                {"uri": uri, "mimeType": result.pop("mimeType"), "text": text},
            ],
            **{k: v for k, v in result.items() if k != "text"},
        }


def build_default_registry(
    knowledge: Any = None,
    persona: Any = None,
    graph: Any = None,
) -> ResourceRegistry:
    """按可用数据源组装 resources 与 prompts。

    Args:
        knowledge: KnowledgeService | None —— 知识库文档
        persona: PersonaConfig | None —— 人格配置（提示词模板）
        graph: GraphStore | None —— 实体图谱统计
    """
    reg = ResourceRegistry()

    if knowledge is not None:
        async def _read_knowledge(uri: str) -> dict[str, Any]:
            # uri 形如 knowledge://doc/{doc_id}
            doc_id = uri.rsplit("/", 1)[-1]
            try:
                docs = await knowledge.list_documents()
            except Exception:
                docs = []
            if doc_id == "index":
                lines = [
                    f"- {d.get('doc_id', '?')}  {d.get('source', '')}"
                    for d in docs
                ]
                return {"text": "\n".join(lines) or "（知识库为空）"}
            hits = await knowledge.recall(doc_id, top_k=3)
            body = "\n\n".join(
                f"[{h.get('source', h.get('doc_id', ''))}]\n{h.get('text', '')}"
                for h in hits
            )
            return {"text": body or f"（未召回 doc_id={doc_id} 的内容）"}

        reg.register_resource(
            Resource(
                uri="knowledge://doc/index",
                name="知识库文档索引",
                description="已导入知识库文档的 doc_id 与来源清单",
            ),
            _read_knowledge,
        )
        reg.register_resource(
            Resource(
                uri="knowledge://doc/{doc_id}",
                name="知识库文档内容",
                description="按 doc_id 召回文档正文（top-3 chunk）",
            ),
            _read_knowledge,
        )

    if persona is not None:
        reg.register_prompt(Prompt(
            name="zhixi_system_prompt",
            description="智犀人格 system prompt 骨架（运行时真实生效配置）",
            arguments=[
                {"name": "name", "description": "人格名", "required": True},
                {"name": "role", "description": "角色定位", "required": True},
                {"name": "style", "description": "说话风格", "required": False},
                {"name": "memory", "description": "记忆召回块", "required": False},
                {"name": "lorebook", "description": "世界观注入块", "required": False},
            ],
            # 占位符用 {{name}}：与 Prompt.render 的替换语法一致。
            # 刻意不用 str.format —— 用户可控参数里带 { 会把整个请求打挂。
            template=(
                "你是{{name}}，{{role}}。说话风格：{{style}}。\n"
                "{{lorebook}}\n{{memory}}\n"
                "始终用简洁、专业、温暖的语气回复用户。"
            ),
        ))
        reg.register_resource(
            Resource(
                uri="persona://config",
                name="人格配置",
                description="当前生效的 PersonaConfig（身份/行为/模块开关）",
                mime_type="application/json",
            ),
            lambda _uri: {
                "text": persona.model_dump_json(indent=2),
                "mimeType": "application/json",
            },
        )

    if graph is not None:
        def _read_graph(_uri: str) -> dict[str, Any]:
            return {
                "text": (
                    f"实体节点数: {graph.entity_count}\n"
                    f"关系边数: {getattr(graph, 'relation_count', 'n/a')}"
                ),
            }

        reg.register_resource(
            Resource(
                uri="graph://stats",
                name="实体图谱统计",
                description="实体图谱的节点/关系规模",
            ),
            _read_graph,
        )

    return reg


# 全局单例（与 MCPToolServer 一样由引擎装配时初始化）
_registry: ResourceRegistry | None = None


def get_resource_registry() -> ResourceRegistry | None:
    return _registry


def set_resource_registry(reg: ResourceRegistry | None) -> None:
    global _registry
    _registry = reg
    if reg is not None:
        log.info(
            "mcp_resources_registered",
            resources=len(reg.list_resources()),
            prompts=len(reg.list_prompts()),
        )


__all__ = [
    "Prompt",
    "Resource",
    "ResourceRegistry",
    "build_default_registry",
    "get_resource_registry",
    "set_resource_registry",
]
