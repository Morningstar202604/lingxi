"use client";

import { useCallback, useEffect, useState } from "react";
import MetricsGrid from "@/components/MetricsGrid";
import QueuePanel from "@/components/QueuePanel";
import TicketsTable from "@/components/TicketsTable";
import {
  fetchQueue,
  fetchSummary,
  fetchTickets,
  type DashboardSummary,
  type QueueSnapshot,
  type TicketSummary,
} from "@/lib/api";

export default function DashboardPage() {
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [queue, setQueue] = useState<QueueSnapshot | null>(null);
  const [recent, setRecent] = useState<TicketSummary[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, q, t] = await Promise.all([
        fetchSummary(),
        fetchQueue(),
        fetchTickets(),
      ]);
      setSummary(s);
      setQueue(q);
      setRecent(t.slice(0, 10));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 5_000);
    return () => clearInterval(id);
  }, [load]);

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center py-20">
        <div className="card border-lingxi-red/30 max-w-md text-center">
          <p className="text-lingxi-red text-sm font-medium">无法连接后端</p>
          <p className="text-xs text-lingxi-muted mt-2">{error}</p>
          <p className="text-xs text-lingxi-muted mt-4">
            请确保智犀后端已启动（端口 8080）
          </p>
          <button
            onClick={load}
            className="mt-4 px-4 py-2 bg-lingxi-orange/10 text-lingxi-orange border border-lingxi-orange/30 rounded-lg text-xs hover:bg-lingxi-orange/20 transition-colors"
          >
            重试
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* 实时指标 */}
      <section>
        <h3 className="text-xs font-medium text-lingxi-muted uppercase tracking-wider mb-3">
          实时指标
        </h3>
        {summary ? (
          <MetricsGrid
            active={summary.active_tickets}
            escalated={summary.escalated}
            processing={summary.processing}
            waiting={summary.waiting_user}
            queuePending={summary.queue.pending_count}
            longestWait={summary.queue.longest_wait}
          />
        ) : (
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="card animate-pulse h-20" />
            ))}
          </div>
        )}
      </section>

      {/* 升级队列 + 最近工单 */}
      <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div>
          <h3 className="text-xs font-medium text-lingxi-muted uppercase tracking-wider mb-3">
            升级队列
          </h3>
          {queue ? (
            <QueuePanel items={queue.top_5_pending} />
          ) : (
            <div className="card animate-pulse h-32" />
          )}
        </div>
        <div>
          <h3 className="text-xs font-medium text-lingxi-muted uppercase tracking-wider mb-3">
            最近活跃工单
          </h3>
          <div className="card">
            {recent.length > 0 ? (
              <TicketsTable tickets={recent} compact />
            ) : (
              <p className="text-xs text-lingxi-muted text-center py-8">
                暂无活跃工单
              </p>
            )}
          </div>
        </div>
      </section>

    </div>
  );
}
