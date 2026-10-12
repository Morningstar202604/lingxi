"use client";

// ── 实时事件流面板 ──
// 数据源：GET /api/events（SSE，升级 / 认领 / 关闭事件）。
//
// 为什么不用 EventSource：它不支持自定义请求头，而该端点要求 x-admin-token。
// 把 token 塞进 query 会进浏览器历史、Nginx 访问日志与 Referer —— 那是降级安全。
// 这里用 fetch + ReadableStream 自己解 SSE 帧，凭据只走请求头。

import { useCallback, useEffect, useRef, useState } from "react";

import { subscribeQueueEvents, type SSEEvent } from "@/lib/api";

const EVENT_LABELS: Record<string, string> = {
  connected: "已连接",
  escalated: "工单升级",
  claimed: "坐席认领",
  completed: "工单完成",
  ping: "心跳",
};

interface FeedItem extends SSEEvent {
  seq: number;
  at: string;
}

function labelOf(event: string): string {
  return EVENT_LABELS[event] ?? event;
}

export default function EventStreamPanel() {
  const [items, setItems] = useState<FeedItem[]>([]);
  const [status, setStatus] = useState<"connecting" | "open" | "closed">("connecting");
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  const connect = useCallback(() => {
    setStatus("connecting");
    setError(null);
    const controller = new AbortController();

    subscribeQueueEvents(
      (ev) => {
        setStatus("open");
        seq.current += 1;
        const item: FeedItem = {
          ...ev,
          seq: seq.current,
          at: new Date().toLocaleTimeString("zh-CN"),
        };
        // 只保留最近 50 条：新事件在顶部插入，长跑不会把 DOM 撑爆
        setItems((prev) => [item, ...prev].slice(0, 50));
      },
      { signal: controller.signal },
    )
      .catch((e: Error) => {
        if (controller.signal.aborted) return;
        setStatus("closed");
        setError(e.message);
      });

    return () => controller.abort();
  }, []);

  useEffect(() => connect(), [connect]);

  const statusDot =
    status === "open" ? "bg-lingxi-green" : status === "connecting" ? "bg-lingxi-orange" : "bg-lingxi-red";

  return (
    <div className="card page-enter">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-medium text-white">实时事件流</h3>
        <div className="flex items-center gap-3">
          <span className="flex items-center gap-1.5 text-xs text-lingxi-muted">
            <span className={`w-1.5 h-1.5 rounded-full ${statusDot} pulse-dot`} />
            {status === "open" ? "已连接" : status === "connecting" ? "连接中" : "已断开"}
          </span>
          <button
            onClick={connect}
            className="px-2 py-1 rounded text-xs border border-white/10 bg-white/5 text-white/70 hover:bg-white/10 transition-colors"
          >
            重连
          </button>
        </div>
      </div>

      {error && (
        <p className="text-xs text-lingxi-red mb-2">
          {error}（后端未启动或凭据无效）
        </p>
      )}

      {items.length === 0 ? (
        <p className="text-xs text-lingxi-muted text-center py-6">
          等待事件…升级 / 认领 / 关闭会实时出现在这里
        </p>
      ) : (
        <ul className="space-y-1.5 max-h-72 overflow-auto">
          {items.map((it) => (
            <li
              key={it.seq}
              className="flex items-start gap-2 text-xs py-1.5 px-2 rounded bg-white/[0.03] border-l-2 border-lingxi-orange/30 animate-slide-in"
            >
              <span className="text-lingxi-muted tabular-nums shrink-0">{it.at}</span>
              <span className="text-lingxi-orange shrink-0 w-16">
                {labelOf(it.event)}
              </span>
              <span className="text-white/70 break-all">
                {typeof it.data === "string" ? it.data : JSON.stringify(it.data)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}