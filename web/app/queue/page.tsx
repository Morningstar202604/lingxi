"use client";

import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  claimTicket,
  completeTicket,
  fetchQueue,
  type QueueSnapshot,
} from "@/lib/api";
import ApiErrorPanel from "@/components/ApiState";
import QueuePanel from "@/components/QueuePanel";

export default function QueuePage() {
  const [queue, setQueue] = useState<QueueSnapshot | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState("");
  const [claiming, setClaiming] = useState(false);

  const load = useCallback(async () => {
    try {
      const q = await fetchQueue();
      setQueue(q);
      setError(null);
    } catch (e) {
      setError(e);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 3_000);
    return () => clearInterval(id);
  }, [load]);

  const handleClaim = useCallback(async () => {
    if (claiming) return;
    setClaiming(true);
    setNotice("");
    try {
      await claimTicket("dashboard-agent");
      await load();
    } catch (e) {
      // 404 = 队列为空（后端对空队列返回 404 而不是空对象）——
      // 这不是错误，是正常状态，别弹整屏红字（AUDIT-2026-10-10 #26）。
      if (e instanceof ApiError && e.status === 404) {
        setNotice("当前没有待认领工单");
        await load();
      } else {
        setError(e);
      }
    } finally {
      setClaiming(false);
    }
  }, [claiming, load]);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="text-xs text-lingxi-muted">
          {queue
            ? `${queue.pending_count} 待认领 · ${queue.claimed_count} 已认领`
            : "加载中..."}
        </div>
        <button
          onClick={handleClaim}
          disabled={claiming || !queue?.pending_count}
          className="px-4 py-2 rounded-lg text-xs border border-lingxi-green/30 bg-lingxi-green/10 text-lingxi-green hover:bg-lingxi-green/20 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {claiming ? "认领中..." : "立即认领"}
        </button>
      </div>

      {notice && (
        <div className="text-xs text-lingxi-muted px-3 py-2 rounded-lg border border-white/10 bg-white/5">
          {notice}
        </div>
      )}

      {error ? (
        <ApiErrorPanel error={error} onRetry={load} compact />
      ) : queue ? (
        <QueuePanel items={queue.top_5_pending} onClaim={handleClaim} />
      ) : (
        <div className="card animate-pulse h-48" />
      )}

      {queue && (
        <div className="grid grid-cols-2 gap-4">
          <div className="card">
            <div className="text-xs text-lingxi-muted">最长等待</div>
            <div className="text-2xl font-bold text-white mt-1">
              {queue.longest_wait}s
            </div>
          </div>
          <div className="card">
            <div className="text-xs text-lingxi-muted">已认领</div>
            <div className="text-2xl font-bold text-white mt-1">
              {queue.claimed_count}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
