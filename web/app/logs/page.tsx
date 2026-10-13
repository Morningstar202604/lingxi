"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchLogs, type LogEntry } from "@/lib/api";
import ApiErrorPanel from "@/components/ApiState";

const levelColor: Record<string, string> = {
  DEBUG: "text-gray-400",
  INFO: "text-lingxi-green",
  WARNING: "text-yellow-400",
  ERROR: "text-lingxi-red",
  CRITICAL: "text-lingxi-red font-bold",
};

export default function LogsPage() {
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [paused, setPaused] = useState(false);
  const autoScroll = useRef(true);
  const container = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    if (paused) return;
    try {
      const data = await fetchLogs(300);
      setLogs(data);
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, [paused]);

  useEffect(() => {
    load();
    const id = setInterval(load, 3_000);
    return () => clearInterval(id);
  }, [load]);

  // 自动滚到底
  useEffect(() => {
    if (autoScroll.current && container.current) {
      container.current.scrollTop = container.current.scrollHeight;
    }
  }, [logs]);

  const onScroll = () => {
    if (!container.current) return;
    const el = container.current;
    autoScroll.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-xs text-lingxi-muted">
          最近 {logs.length} 条日志
        </div>
        <button
          onClick={() => setPaused((p) => !p)}
          className={`px-3 py-1.5 rounded-lg text-xs border transition-colors ${
            paused
              ? "bg-lingxi-red/10 text-lingxi-red border-lingxi-red/30"
              : "bg-lingxi-green/10 text-lingxi-green border-lingxi-green/30"
          }`}
        >
          {paused ? "⏸ 已暂停" : "▶ 实时刷新"}
        </button>
      </div>

      {error ? (
        <ApiErrorPanel error={error} onRetry={load} compact />
      ) : (
        <div
          ref={container}
          onScroll={onScroll}
          className="card font-mono text-[11px] leading-relaxed overflow-y-auto"
          style={{ height: "calc(100vh - 200px)", minHeight: 300 }}
        >
          {loading && logs.length === 0 ? (
            <div className="text-lingxi-muted p-4 animate-pulse">
              加载中...
            </div>
          ) : logs.length === 0 ? (
            <div className="text-lingxi-muted p-4 text-center">
              暂无日志（请确认后端已启动并开启日志写入内存缓冲区）
            </div>
          ) : (
            logs.map((entry, i) => (
              <div
                key={`${entry.time}-${i}`}
                className="py-0.5 flex gap-2 hover:bg-white/[0.02] px-2"
              >
                <span className="text-lingxi-muted shrink-0 w-16">
                  {entry.time.slice(11, 19)}
                </span>
                <span
                  className={`shrink-0 w-14 ${
                    levelColor[entry.level] || "text-gray-400"
                  }`}
                >
                  {entry.level}
                </span>
                <span className="text-lingxi-muted shrink-0 truncate max-w-48">
                  {entry.logger}
                </span>
                <span className="text-white/80 truncate">{entry.msg}</span>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
