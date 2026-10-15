"""检索重排（rerank）— 基于 **flashrank**（本地 ONNX，零配置零 API key）。

🔗 工程逻辑
    混合召回（向量 + BM25 + RRF）解决的是"能不能捞到"，rerank 解决的是
    "捞到的排序对不对"。业界共识：**召回后加一层 cross-encoder 重排是
    RAG 质量最高性价比的单点提升** —— 向量检索是双塔（query/doc 各自
    编码，交互信息丢失），cross-encoder 让 query 与 doc 逐 token 交互，
    排序精度显著更高。

    选 flashrank 而不是 Cohere Rerank API：
    - 本地 ONNX 推理，**无需 API key**（与项目"零配置可用"一致）
    - 模型体积小（几 MB，首次使用自动下载），CPU 可跑
    - 想要云端更强效果时可换 Cohere（配置项预留）

🤖 AI 避坑
    - 模型首次加载会下载权重（几 MB）→ 懒加载 + 失败降级为原始排序，
      绝不能因为 rerank 挂了就让检索整体失败。
    - rerank 只该重排**已召回的候选**，不要用它替代召回（cross-encoder
      是 O(n) 逐个打分，全库打分不可行）。
    - 线程池执行：flashrank 是同步 CPU 推理，在 async 路径里直接调会阻塞
      事件循环。
"""
from __future__ import annotations

import asyncio
from typing import Any

from lingxi.core import get_logger

log = get_logger(__name__)

_ranker: Any = None
_load_failed: str | None = None


def _get_ranker(model_name: str, cache_dir: str) -> Any:
    """懒加载 flashrank Ranker（首次会下载模型，失败则记住原因不再重试）。

    cache_dir 显式传入：默认落在 `data/rerank_models/`（跟着项目数据目录走），
    而不是 HOME 缓存 —— 便于运维预热/离线部署（把模型目录放到数据盘即可），
    也避免"换用户跑就重新下载"。
    """
    global _ranker, _load_failed
    if _ranker is not None:
        return _ranker
    if _load_failed is not None:
        return None
    try:
        from flashrank import Ranker

        _ranker = Ranker(model_name=model_name, cache_dir=cache_dir)
        log.info("reranker_loaded", backend="flashrank", model=model_name, cache_dir=cache_dir)
        return _ranker
    except Exception as e:  # noqa: BLE001 — 模型下载/依赖缺失都只降级
        _load_failed = f"{type(e).__name__}: {e}"
        log.warning(
            "reranker_unavailable_fallback_original_order",
            error=_load_failed,
            hint=(
                "降级为融合原始排序（功能不受影响）。离线环境可手动预置："
                f"把模型解压到 {cache_dir}/{model_name}/；"
                "国内网络可用 HF 镜像 https://hf-mirror.com/prithivida/flashrank/resolve/main/"
            ),
        )
        return None


def _sync_rerank(
    query: str,
    candidates: list[dict[str, Any]],
    top_k: int,
    model_name: str,
    cache_dir: str,
) -> list[dict[str, Any]]:
    """同步重排（在 to_thread 里跑，避免阻塞事件循环）。"""
    ranker = _get_ranker(model_name, cache_dir)
    if ranker is None:
        return candidates[:top_k]

    from flashrank import RerankRequest

    passages = [
        {"id": str(i), "text": c.get("text", "")} for i, c in enumerate(candidates)
    ]
    ranked = ranker.rerank(RerankRequest(query=query, passages=passages))

    out: list[dict[str, Any]] = []
    for item in ranked[:top_k]:
        try:
            idx = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= idx < len(candidates):
            hit = dict(candidates[idx])
            # rerank 分数是 cross-encoder 相关性（0-1），覆盖掉融合分
            if "score" in item:
                hit["score"] = round(float(item["score"]), 4)
            hit["reranked"] = True
            out.append(hit)
    return out or candidates[:top_k]


async def rerank(
    query: str,
    candidates: list[dict[str, Any]],
    top_k: int = 5,
    *,
    enabled: bool = True,
    model_name: str = "ms-marco-MiniLM-L-12-v2",
    cache_dir: str = "",
) -> list[dict[str, Any]]:
    """对召回候选做 cross-encoder 重排，返回 top_k。

    失败（依赖缺失/模型下载失败/推理异常）一律**降级为原始顺序**，
    只记 warning —— 检索可用性永远优先于排序质量。
    """
    if not enabled or not candidates:
        return candidates[:top_k]
    if len(candidates) == 1:
        return candidates[:top_k]
    if not cache_dir:
        # 默认跟项目数据目录（便于离线部署与预热）
        from lingxi.config import get_settings

        cache_dir = str(get_settings().data_dir / "rerank_models")
    try:
        return await asyncio.to_thread(
            _sync_rerank, query, candidates, top_k, model_name, cache_dir
        )
    except Exception as e:  # noqa: BLE001
        log.warning("rerank_failed_fallback_original_order", error=str(e))
        return candidates[:top_k]


def reset_for_tests() -> None:
    """清掉模型缓存（测试用）。"""
    global _ranker, _load_failed
    _ranker = None
    _load_failed = None


def rerank_status() -> dict[str, Any]:
    """rerank 可用状态（健康检查/排障）。"""
    return {
        "loaded": _ranker is not None,
        "load_failed_reason": _load_failed or "",
    }


__all__ = ["rerank", "rerank_status", "reset_for_tests"]
