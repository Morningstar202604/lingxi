"""模型接入状态子路由（/api/setup/status）。

🔗 工程逻辑
    从 dashboard_api.py 拆出（AUDIT-2026-10-10 第四章 4.5：单文件承载
    端点+认证+序列化会失控）。create_dashboard_app 只负责调用
    `register_setup_routes(app, check_admin)`，凭据策略沿用宿主 app 的
    check_admin 闭包；Ollama 探测缓存（10s TTL）挂在本模块，进程内共享。

🤖 AI 避坑
    - 探测走 stdlib urllib（不为此引入新依赖），1.5s 超时、失败即不可达
    - 云端预设只是"可复制片段"，不读取也不回写任何密钥
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Header

from lingxi.config import get_settings

if TYPE_CHECKING:
    from collections.abc import Callable

# 云端预设：provider=openai 用于 OpenAI 兼容端点（DashScope / 智谱）
_CLOUD_PRESETS: list[dict[str, Any]] = [
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "tag": "国内推荐 · 便宜",
        "provider": "deepseek",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "env": {
            "LINGXI_LLM__PROVIDER": "deepseek",
            "LINGXI_LLM__MODEL": "deepseek-chat",
            "LINGXI_LLM__API_KEY": "<到官网申请你的 Key>",
            "LINGXI_LLM__BASE_URL": "https://api.deepseek.com",
        },
        "key_page": "https://platform.deepseek.com/api_keys",
    },
    {
        "id": "qwen",
        "label": "通义千问（DashScope）",
        "tag": "OpenAI 兼容端点",
        "provider": "openai",
        "model": "qwen-plus",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "env": {
            "LINGXI_LLM__PROVIDER": "openai",
            "LINGXI_LLM__MODEL": "qwen-plus",
            "LINGXI_LLM__API_KEY": "<DashScope API Key>",
            "LINGXI_LLM__BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        },
        "key_page": "https://bailian.console.aliyun.com/",
    },
    {
        "id": "zhipu",
        "label": "智谱 GLM",
        "tag": "有免费档位",
        "provider": "openai",
        "model": "glm-4-flash",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "env": {
            "LINGXI_LLM__PROVIDER": "openai",
            "LINGXI_LLM__MODEL": "glm-4-flash",
            "LINGXI_LLM__API_KEY": "<到开放平台申请>",
            "LINGXI_LLM__BASE_URL": "https://open.bigmodel.cn/api/paas/v4",
        },
        "key_page": "https://open.bigmodel.cn/usercenter/apikeys",
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "tag": "国际通用",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "base_url": "",
        "env": {
            "LINGXI_LLM__PROVIDER": "openai",
            "LINGXI_LLM__MODEL": "gpt-4o-mini",
            "LINGXI_LLM__API_KEY": "<sk-...>",
            "LINGXI_LLM__BASE_URL": "",
        },
        "key_page": "https://platform.openai.com/api-keys",
    },
]


# Ollama 探测缓存（10s TTL，页面轮询不空跑）
_probe_cache: dict[str, Any] = {"at": 0.0, "ollama": None}


async def _probe_ollama(native_base: str) -> dict[str, Any]:
    """探测本地 Ollama（stdlib，不新增依赖；1.5s 超时，失败即不可达）。"""

    def _sync() -> dict[str, Any]:
        req = urllib.request.Request(f"{native_base}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=1.5) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
        models = [
            str(m.get("name", ""))
            for m in body.get("models", [])
            if isinstance(m, dict)
        ]
        return {"reachable": True, "base_url": native_base, "models": models[:20]}

    return await asyncio.to_thread(_sync)


def register_setup_routes(
    app: FastAPI, check_admin: Callable[[str | None], None]
) -> None:
    """注册 /api/setup/status。check_admin 沿用宿主 app 的凭据策略。"""

    @app.get("/api/setup/status")
    async def setup_status(x_admin_token: str | None = Header(None)) -> dict[str, Any]:
        """模型接入现状：当前 LLM 配置、Ollama 可达性、云端预设片段。"""
        check_admin(x_admin_token)
        s = get_settings()
        llm = s.llm
        api_key_configured = bool(
            llm.api_key and llm.api_key.get_secret_value().strip()
        )

        native_base = "http://127.0.0.1:11434"
        if llm.base_url and "11434" in llm.base_url:
            native_base = llm.base_url.rstrip("/")
            if native_base.endswith("/v1"):
                native_base = native_base[:-3]

        now = time.monotonic()
        if _probe_cache["ollama"] is None or now - _probe_cache["at"] > 10.0:
            try:
                probed = await _probe_ollama(native_base)
            except Exception:
                probed = {"reachable": False, "base_url": native_base, "models": []}
            _probe_cache["ollama"] = probed
            _probe_cache["at"] = now
        ollama = _probe_cache["ollama"]

        ready = api_key_configured or (llm.provider == "ollama" and ollama["reachable"])
        # 推荐策略：线上 API 优先（质量与稳定性都更好），本地模型作为可选。
        # 前端据此决定引导文案；不替用户做决定，只标注推荐。
        if api_key_configured and llm.provider != "ollama":
            mode = "cloud"          # 已按推荐方式配置
        elif llm.provider == "ollama" and ollama["reachable"]:
            mode = "local_ready"    # 本地可用，但建议考虑云端
        elif api_key_configured:
            mode = "cloud"
        else:
            mode = "unconfigured"   # 什么都没配 → 首要引导配云端
        return {
            "llm": {
                "provider": llm.provider,
                "model": llm.model,
                "base_url": llm.base_url,
                "api_key_configured": api_key_configured,
                "ready": ready,
                "mode": mode,
            },
            "recommendation": {
                "primary": "cloud",
                "text": (
                    "推荐使用云端 API：回答质量更高、免下载、按量计费成本低。"
                    "本地模型（Ollama）完全可选 —— 不安装不影响使用，"
                    "需要离线/隐私场景时再装。"
                ),
            },
            "ollama": {
                **ollama,
                "install_url": "https://ollama.com/download",
                "pull_hint": f"ollama pull {s.mem0.embed_model} && ollama pull {llm.model}",
            },
            "cloud_presets": _CLOUD_PRESETS,
            "env_file_hint": (
                "把环境变量写进项目根目录 .env（已支持），改完重启后端生效；"
                r"桌面版可写进 %APPDATA%\lingxi\lingxi.env"
            ),
        }


__all__ = ["register_setup_routes"]
