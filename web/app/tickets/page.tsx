"use client";

import { useCallback, useEffect, useState } from "react";
import {
  categoryLabel,
  channelLabel,
  fetchTickets,
  statusLabel,
  type TicketSummary,
} from "@/lib/api";
import ApiErrorPanel from "@/components/ApiState";

const STATUS_FILTERS = [
  { key: "", label: "全部" },
  { key: "intake", label: "接入中" },
  { key: "processing", label: "处理中" },
  { key: "waiting_user", label: "等用户" },
  { key: "escalated", label: "已升级" },
  { key: "resolved", label: "已解决" },
  { key: "closed", label: "已关闭" },
];

const statusColor: Record<string, string> = {
  intake: "bg-blue-500/10 text-blue-400 border-blue-500/25",
  processing: "bg-lingxi-orange/10 text-lingxi-orange border-lingxi-orange/25",
  waiting_user: "bg-yellow-500/10 text-yellow-400 border-yellow-500/25",
  escalated: "bg-lingxi-red/10 text-lingxi-red border-lingxi-red/25",
  resolved: "bg-lingxi-green/10 text-lingxi-green border-lingxi-green/25",
  closed: "bg-gray-500/10 text-gray-400 border-gray-500/25",
};

export default function TicketsPage() {
  const [tickets, setTickets] = useState<TicketSummary[]>([]);
  const [filter, setFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    try {
      setLoading(true);
      const data = await fetchTickets(filter || undefined);
      setTickets(data);
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    load();
    const id = setInterval(load, 5_000);
    return () => clearInterval(id);
  }, [load]);

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 flex-wrap">
        {STATUS_FILTERS.map((f) => (
          <button
            key = {f.key}
            onClick={() => setFilter(f.key)}
            className = {`px-3 py-1.5 rounded-lg text-xs border transition-colors ${
              filter === f.key
                ? "bg-lingxi-orange/15 text-lingxi-orange border-lingxi-orange/40"
                : "bg-white/5 text-white/50 border-white/10 hover:bg-white/10"
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {error ? (
        <ApiErrorPanel error={error} onRetry={load} compact />
      ) : (
        <div className="card overflow-hidden p-0">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left border-b border-white/5 text-lingxi-muted bg-white/[0.03]">
                <th className="px-4 py-3 font-medium">工单 ID</th>
                <th className="px-4 py-3 font-medium">渠道</th>
                <th className="px-4 py-3 font-medium">分类</th>
                <th className="px-4 py-3 font-medium">状态</th>
                <th className="px-4 py-3 font-medium text-right">消息数</th>
              </tr>
            </thead>
            <tbody>
              {loading && tickets.length === 0 ? (
                Array.from({ length: 6 }).map((_, i) => (
                  <tr key={i} className="border-b border-white/5 animate-pulse">
                    <td colSpan={5} className="px-4 py-3">
                      <div className="h-4 bg-white/5 rounded" />
                    </td>
                  </tr>
                ))
              ) : tickets.length === 0 ? (
                <tr>
                  <td colSpan={5} className="px-4 py-12 text-center text-lingxi-muted">
                    当前筛选条件下暂无工单
                  </td>
                </tr>
              ) : (
                tickets.map((t) => (
                  <tr
                    key={t.ticket_id}
                    className="border-b border-white/5 hover:bg-white/[0.02] transition-colors"
                  >
                    <td className="px-4 py-3 font-mono text-lingxi-muted">
                      {t.ticket_id.slice(0, 12)}
                    </td>
                    <td className="px-4 py-3">{channelLabel(t.channel)}</td>
                    <td className="px-4 py-3">{categoryLabel(t.category)}</td>
                    <td className="px-4 py-3">
                      <span
                        className={`px-1.5 py-0.5 rounded border text-[10px] ${
                          statusColor[t.status] || statusColor.closed
                        }`}
                      >
                        {statusLabel(t.status)}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-right text-lingxi-muted">
                      {t.message_count}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
          {!loading && tickets.length > 0 && (
            <div className="px-4 py-2 text-[10px] text-lingxi-muted border-t border-white/5">
              共 {tickets.length} 条 · 5 秒自动刷新
            </div>
          )}
        </div>
      )}
    </div>
  );
}
