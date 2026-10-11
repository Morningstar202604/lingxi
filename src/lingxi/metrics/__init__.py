"""Prometheus 指标模块。

🔗 工程逻辑
    提供统一的指标收集器（MetricsCollector 单例），
    供中间件/业务代码调用，暴露 /metrics 端点供 Prometheus scraper 抓取。

    - get_metrics_collector(): 懒加载单例工厂
    - MetricsCollector: 封装 prometheus_client 的 Counter/Histogram/Gauge

🤖 AI 避坑
    - 不要在模块顶层直接实例化 MetricsCollector（有 I/O 和依赖检查）
    - 用 get_metrics_collector() 获取单例，保证全局标签一致性
    - 本模块 import 不强制要求 prometheus_client 安装（降级 noop）
"""
from lingxi.metrics.collector import MetricsCollector, get_metrics_collector

__all__ = [
    "get_metrics_collector",
    "MetricsCollector",
]
