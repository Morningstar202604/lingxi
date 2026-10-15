"""LLM 路由与预算 —— **litellm 官方 Router**（多 key 轮转 + 熔断 + 预算）。

🔗 工程逻辑
    单 key + 手写 fallback 链能跑，但缺三样生产必需能力：

    1. **多 key 轮转**：一个 key 打满配额/被限流时自动换下一个，
       `routing_strategy` 决定选谁（simple-shuffle / least-busy /
       latency-based-routing）。
    2. **熔断（cooldown）**：某次部署连续失败 N 次后自动摘除一段时间，
       不再把请求打到已知坏掉的端点上。
    3. **成本预算**：按 request/token 记账，超预算直接阻断 ——
       这是"防止跑飞"的最后一道闸。

    这些全是 litellm `Router` 的内置能力，不必自己写轮转表/熔断器
    （自研熔断几乎必错：时间窗、半开状态、并发下的计数竞争都要处理）。

    🤖 AI 避坑
        - **单 key 时必须不启用 Router**：Router 会重新包装调用路径，
          对零配置/本地 Ollama 场景纯属增加复杂度与不确定性。
          只有配了多个凭据（或显式开启）才构建 Router。
        - 预算单位是美元；本项目按"估算成本"记账（litellm 用内置价格表
          推算），未配价格表的自建端点成本为 0，预算约束对它们无效 ——
          这应当在文档里说清，不要让人误以为"设了预算就万无一失"。
        - Router 的 `model_group` 概念：同名部署成一组，Router 在组内
          挑选，组间不互通。
"""
from __future__ import annotations

from typing import Any

from lingxi.core import get_logger

log = get_logger(__name__)

_router: Any = None
_router_signature: str = ""


def _deployment_list(settings: Any) -> list[dict[str, Any]]:
    """按配置生成部署列表。

    支持两种形态（都映射成 Router 的 model_list）：
    1. 单 key + fallback_models → 每个模型一个部署（同 key）
    2. 多 key（llm.api_keys）→ 同一 model_group 下多个部署，key 分散
    """
    llm = settings.llm
    provider = llm.provider
    base_url = llm.base_url

    # 多 key：config 里给了 keys 列表就一 key 一部署（同组内轮转）
    # SecretStr 必须解包成明文再交给 litellm（否则 pydantic 校验报
    # "Input should be a valid string, input_type=SecretStr"）
    extra_keys: list[str] = []
    raw_keys = getattr(llm, "api_keys", None)
    if isinstance(raw_keys, list):
        for k in raw_keys:
            if not k:
                continue
            extra_keys.append(k.get_secret_value() if hasattr(k, "get_secret_value") else str(k))

    primary_key = llm.api_key.get_secret_value() if llm.api_key else ""
    keys = [primary_key, *extra_keys] if primary_key else list(extra_keys)
    if not keys:
        keys = [""]  # 本地模型（ollama）无需 key

    models = [llm.model, *[m for m in llm.fallback_models if m != llm.model]]
    deployments: list[dict[str, Any]] = []
    for model in models:
        for key in keys:
            params: dict[str, Any] = {"model": f"{provider}/{model}" if provider else model}
            if key:
                params["api_key"] = key
            if base_url:
                # provider 归一化：ollama 去掉 /v1（litellm 走原生 API）
                params["api_base"] = _normalize_base(provider, base_url)
            deployments.append({"model_name": model, "litellm_params": params})
    return deployments


def _normalize_base(provider: str, base_url: str) -> str:
    """provider 归一化 base_url（ollama 去掉 /v1，与 llm.py 同一实现）。"""
    from lingxi.agent.llm import resolve_base_url

    return resolve_base_url(provider, base_url) or base_url


def _construct_router(router_cls: Any, kwargs: dict[str, Any]) -> Any:
    """构造 Router，必要时为其提供事件循环。

    🔗 工程逻辑
        litellm 的预算（`provider_budget_config`）在构造时会启动后台检查
        任务，**必须在事件循环内**；纯同步上下文（脚本/测试）直接构造会
        报 "no running event loop"（实测）。

        这里在无运行中 loop 时**临时起一个并保留**：预算检查的后台任务
        需要长期存活，关掉 loop 会让预算失效。因此在模块级持有一个
        专用于此的后台 loop 线程（与 flow/postgres 的 _AsyncRunner 同思路，
        但更轻量——只为让 litellm 的后台任务有归属）。
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 无运行中 loop：用常驻后台 loop（不关闭，供 litellm 后台任务存活）
        loop = _ensure_background_loop()
        if loop is not None:
            fut = asyncio.run_coroutine_threadsafe(_async_construct(router_cls, kwargs), loop)
            return fut.result(timeout=30)
    return router_cls(**kwargs)


async def _async_construct(router_cls: Any, kwargs: dict[str, Any]) -> Any:
    return router_cls(**kwargs)


_bg_loop: Any = None
_bg_thread: Any = None


def _ensure_background_loop() -> Any:
    """惰性创建常驻后台事件循环（供预算检查等后台任务使用）。"""
    global _bg_loop, _bg_thread
    if _bg_loop is not None:
        return _bg_loop
    import asyncio
    import threading

    loop = asyncio.new_event_loop()

    def _run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    t = threading.Thread(target=_run, name="llm-router-loop", daemon=True)
    t.start()
    _bg_loop = loop
    _bg_thread = t
    return loop


def should_enable_router(settings: Any) -> bool:
    """是否值得构建 Router。

    只在"多 key"或"显式开启"时启用 —— 单 key 本地模型用 Router 是负收益
    （多一层包装、启动更慢、行为更难预测）。
    """
    llm = settings.llm
    if bool(getattr(llm, "router_enabled", False)):
        return True
    raw_keys = getattr(llm, "api_keys", None)
    return isinstance(raw_keys, list) and len([k for k in raw_keys if k]) >= 1


def build_router(settings: Any) -> Any:
    """按官方 Router 构建（多 key 轮转 + 熔断 + 可选预算）。返回 None 表示不启用。"""
    global _router, _router_signature
    if not should_enable_router(settings):
        log.info(
            "llm_router_disabled",
            reason="单 key 场景无需 Router（避免多一层包装）",
            hint="配 LINGXI_LLM__API_KEYS='[\"k1\",\"k2\"]' 或 LINGXI_LLM__ROUTER_ENABLED=true 启用",
        )
        return None

    llm = settings.llm
    deployments = _deployment_list(settings)
    # signature 必须含"影响 Router 行为的全部配置"，否则改了 strategy/预算
    # 却复用旧实例（实测踩到：strategy 改动不生效）
    signature = repr(
        (
            deployments,
            getattr(llm, "routing_strategy", ""),
            getattr(llm, "allowed_fails", 0),
            getattr(llm, "cooldown_time", 0),
            getattr(llm, "max_budget_usd", None),
        )
    )
    if _router is not None and signature == _router_signature:
        return _router

    try:
        from litellm import Router  # type: ignore[attr-defined]  # litellm 未显式导出

        kwargs: dict[str, Any] = {
            "model_list": deployments,
            "num_retries": int(getattr(settings.agent, "llm_max_retries", 2)),
            "timeout": 30,
            "routing_strategy": str(getattr(llm, "routing_strategy", "simple-shuffle")),
            # 熔断：连续失败 allowed_fails 次 → 摘除 cooldown_time 秒
            "allowed_fails": int(getattr(llm, "allowed_fails", 3)),
            "cooldown_time": float(getattr(llm, "cooldown_time", 60.0)),
        }
        # 预算：litellm 的官方接口是 provider_budget_config（不是 max_budget —
        # 实测传 max_budget 会 TypeError 导致 Router 构建失败）。
        # 格式：{provider: {"budget_duration": "30d", "max_budget": <usd>}}
        max_budget = getattr(llm, "max_budget_usd", None)
        if max_budget:
            # ⚠️ litellm 的 provider_budget_config 会在构造时启动后台预算检查
            # 任务，**必须在事件循环内构建**；否则报 "no running event loop"
            # （实测）。同步上下文（脚本/测试直接调用）时先起一个临时 loop。
            budget_cfg: dict[str, Any] = {
                str(llm.provider): {
                    "budget_duration": str(getattr(llm, "budget_duration", "30d")),
                    "max_budget": float(max_budget),
                }
            }
            kwargs["provider_budget_config"] = budget_cfg

        _router = _construct_router(Router, kwargs)
        _router_signature = signature
        log.info(
            "llm_router_enabled",
            deployments=len(deployments),
            strategy=kwargs["routing_strategy"],
            keys=len({d["litellm_params"].get("api_key", "") for d in deployments}),
            max_budget_usd=max_budget or "unlimited",
        )
        return _router
    except Exception as e:  # noqa: BLE001 — Router 构建失败退回直连（不阻断业务）
        log.warning("llm_router_build_failed_fallback_direct", error=str(e))
        return None


def get_router() -> Any:
    """取已构建的 Router（未启用/构建失败时为 None）。"""
    return _router


async def router_acompletion(messages: list[dict[str, Any]], **kwargs: Any) -> Any:
    """通过 Router 调用（多 key 轮转 / 熔断 / 预算都在这一层生效）。"""
    router = get_router()
    if router is None:
        raise RuntimeError("Router 未启用")
    return await router.acompletion(messages=messages, **kwargs)


def router_status() -> dict[str, Any]:
    """Router 状态（健康检查/排障）。"""
    if _router is None:
        return {"enabled": False}
    try:
        model_list = getattr(_router, "model_list", [])
        return {
            "enabled": True,
            "deployments": len(model_list),
            "strategy": getattr(_router, "routing_strategy", ""),
        }
    except Exception:  # noqa: BLE001
        return {"enabled": True}


def reset_for_tests() -> None:
    global _router, _router_signature
    _router = None
    _router_signature = ""


__all__ = [
    "build_router",
    "get_router",
    "reset_for_tests",
    "router_acompletion",
    "router_status",
    "should_enable_router",
]
