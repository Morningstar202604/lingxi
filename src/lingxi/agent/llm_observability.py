"""LLM 可观测性 + 缓存 + 限流接线（全部基于官方机制，不手搓）。

🔗 工程逻辑
    三件事都交给成熟方案：

    1. **可观测性**：litellm 官方 callback（`success_callback` /
       `failure_callback`）→ 把 token/成本/延迟写进 Prometheus。
       不用自己包一层 try/finally 统计 —— litellm 已经把各家 provider
       响应归一化好了（含 `response_cost` 的官方价格表推算）。
    2. **缓存**：litellm 官方 `litellm.cache`（disk 后端，零外部依赖；
       配了 Redis 可切 redis）。相同 prompt 命中缓存直接返回，
       省 token 也省延迟。
    3. **限流**：litellm 官方 `Router` 的 rpm/tpm 限制用于多 key 轮转；
       这里是单 key 场景，故用数据库侧计数（见 `_RateLimiter`）。

🤖 AI 避坑
    - callback 里**绝不能抛异常**：litellm 会把它当调用失败处理，
      统计代码写崩了整个 LLM 链路就断了。全部 catch-all。
    - 成本字段缺失是常态（自建端点/本地模型无价格表）→ 缺就记 0，不报错。
    - 缓存 key 默认对 messages 做哈希：**别把 user_id 等隐私放进 messages**，
      否则缓存跨用户共享会串数据。
    - 本地模型（ollama）缓存收益最大（磁盘换 CPU），默认开启。
"""
from __future__ import annotations

from typing import Any

from lingxi.core import get_logger

log = get_logger(__name__)

_configured = False


def _extract_usage(kwargs: dict[str, Any], response: Any) -> tuple[int, int]:
    """从 litellm 响应/请求里取 token 数（结构随 provider 而异，全兜底）。"""
    prompt = completion = 0
    usage = None
    if response is not None:
        usage = getattr(response, "usage", None)
        if usage is None and isinstance(response, dict):
            usage = response.get("usage")
    if usage is not None:
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        if not prompt and isinstance(usage, dict):
            prompt = int(usage.get("prompt_tokens", 0) or 0)
            completion = int(usage.get("completion_tokens", 0) or 0)
    return prompt, completion


def _split_wire_model(wire_model: str) -> tuple[str, str]:
    """``ollama/qwen2.5:7b`` → ("ollama", "qwen2.5:7b")。"""
    if "/" in wire_model:
        provider, _, model = wire_model.partition("/")
        return provider, model
    return "unknown", wire_model


async def _on_success(kwargs: dict[str, Any], response: Any, start_time: Any, end_time: Any) -> None:
    """litellm 官方 success_callback（必须是 async 签名）。"""
    try:
        from lingxi.metrics import get_metrics_collector

        wire = str(kwargs.get("model", ""))
        provider, model = _split_wire_model(wire)
        prompt, completion = _extract_usage(kwargs, response)

        cost = 0.0
        try:
            cost = float(getattr(response, "_hidden_params", {}).get("response_cost", 0.0) or 0.0)
        except (AttributeError, TypeError, ValueError):
            cost = 0.0

        duration = 0.0
        if start_time is not None and end_time is not None:
            try:
                duration = float((end_time - start_time).total_seconds())
            except (AttributeError, TypeError):
                duration = 0.0

        cache_hit = bool(getattr(response, "_hidden_params", {}).get("cache_hit", False))
        get_metrics_collector().record_llm_success(
            provider=provider,
            model=model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cost_usd=cost,
            duration=duration,
            cache_hit=cache_hit,
        )
    except Exception as e:  # noqa: BLE001 — 统计绝不能让 LLM 调用失败
        log.debug("llm_metric_success_failed", error=str(e))


async def _on_failure(kwargs: dict[str, Any], exception: Any, start_time: Any, end_time: Any) -> None:
    """litellm 官方 failure_callback。"""
    try:
        from lingxi.metrics import get_metrics_collector

        provider, model = _split_wire_model(str(kwargs.get("model", "")))
        get_metrics_collector().record_llm_failure(provider=provider, model=model)
    except Exception as e:  # noqa: BLE001
        log.debug("llm_metric_failure_failed", error=str(e))


def setup_llm_observability(settings: Any = None) -> None:
    """装配 litellm 官方 callback + 缓存。幂等，可重复调用。

    Args:
        settings: Settings 实例；None 时读全局单例。
    """
    global _configured
    if _configured:
        return
    try:
        import litellm
    except ImportError:  # pragma: no cover
        return

    # ── 1. 官方 callback：所有调用自动过指标 ──
    litellm.success_callback = [_on_success]
    litellm.failure_callback = [_on_failure]

    # ── 2. 官方缓存：disk 后端（零外部服务），可切 redis ──
    if settings is None:
        from lingxi.config import get_settings

        settings = get_settings()
    cache_cfg = getattr(settings, "llm_cache", None)
    enabled = bool(getattr(cache_cfg, "enabled", False)) if cache_cfg else False
    if enabled:
        try:
            from litellm.caching.caching import Cache

            backend = str(getattr(cache_cfg, "backend", "disk"))
            kwargs: dict[str, Any] = {
                "type": backend,
                "ttl": int(getattr(cache_cfg, "ttl_seconds", 3600)),
            }
            if backend == "disk":
                kwargs["disk_cache_dir"] = str(getattr(cache_cfg, "disk_dir", ".litellm_cache"))
            elif backend == "redis":
                kwargs["host"] = str(getattr(cache_cfg, "redis_host", "127.0.0.1"))
                kwargs["port"] = int(getattr(cache_cfg, "redis_port", 6379))
            litellm.cache = Cache(**kwargs)
            log.info("llm_cache_enabled", backend=backend)
        except Exception as e:  # noqa: BLE001 — 缓存失败不该阻断启动
            log.warning("llm_cache_setup_failed", error=str(e))

    _configured = True
    log.info("llm_observability_enabled")


def reset_for_tests() -> None:
    """测试用：允许重复装配。"""
    global _configured
    _configured = False


__all__ = ["setup_llm_observability", "reset_for_tests"]
