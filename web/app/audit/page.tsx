"use client";

import { useCallback, useEffect, useState } from "react";
import {
  exportAudit,
  fetchAuditLogs,
  type AuditEntry,
} from "@/lib/api";
import ApiErrorPanel from "@/components/ApiState";

const ACTIONS = [
  "",
  "ticket.created",
  "ticket.transition",
  "ticket.escalated",
  "ticket.closed",
  "queue.enqueue",
  "queue.dequeue",
  "queue.complete",
  "rule.triggered",
  "user.login",
  "config.change",
  "knowledge.upload",
  "knowledge.delete",
];

function fmtTs(unixSec: number): string {
  if (!unixSec) return "-";
  return new Date(unixSec * 1000).toLocaleString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
}

export default function AuditPage() {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [actionFilter, setActionFilter] = useState("");
  const [actorFilter, setActorFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    try {
      const data = await fetchAuditLogs(actionFilter, actorFilter, 200);
      setEntries(data);
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, [actionFilter, actorFilter]);

  useEffect(() => {
    load();
    const id = setInterval(load, 5_000);
    return () => clearInterval(id);
  }, [load]);

  const onExport = async () => {
    await exportAudit();
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h1 className="text-base font-semibold text-white">审计日志</h1>
        <button
          onClick={onExport}
          className="text-xs px-3 py-1.5 rounded bg-lingxi-orange/20 border border-lingxi-orange/40 text-lingxi-orange hover:bg-lingxi-orange/30 transition-colors"
        >
          导出 JSON
        </button>
      </div>

      <div className="flex items-center gap-3 flex-wrap">
        <select
          value={actionFilter}
          onChange={(e) => setActionFilter(e.target.value)}
          className="bg-white/5 border border-white/10 text-white text-xs rounded px-2 py-1.5"
        >
          {ACTIONS.map((a) => (
            <option key={a || "all"} value={a} className="bg-lingxi-navy">
              {a || "全部 action"}
            </option>
          ))}
        </select>
        <input
          type="text"
          value={actorFilter}
          onChange={(e) => setActorFilter(e.target.value)}
          placeholder="按 actor 过滤"
          className="bg-white/5 border border-white/10 text-white text-xs rounded px-2 py-1.5 w-40 placeholder:text-white/30"
        />
        <div className="w-2 h-2 rounded-full bg-lingxi-green pulse-dot" />
        <span className="text-[10px] text-lingxi-muted">每 5s 自动刷新</span>
      </div>

      {error ? (
        <ApiErrorPanel error={error} onRetry={load} compact />
      ) : entries.length === 0 && !loading ? (
        <div className="card text-center py-12">
          <p className="text-sm text-lingxi-muted">暂无审计记录</p>
          <p className="text-xs text-lingxi-muted/60 mt-1">
            每当系统发生关键事件时, 这里会追加记录 (不可篡改)
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-white/10">
          <table className="w-full text-xs min-w-[640px]">
            <thead className="bg-white/5 text-white/60">
              <tr>
                <th className="px-3 py-2 text-left font-medium">时间</th>
                <th className="px-3 py-2 text-left font-medium">Action</th>
                <th className="px-3 py-2 text-left font-medium">Actor</th>
                <th className="px-3 py-2 text-left font-medium">Target</th>
                <th className="px-3 py-2 text-left font-medium">Detail</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e, i) => (
                <tr
                  key={`${e.timestamp}-${e.action}-${i}`}
                  className="border-t border-white/5 hover:bg-white/5 transition-colors"
                >
                  <td className="px-3 py-2 text-lingxi-muted whitespace-nowrap">
                    {fmtTs(e.timestamp)}
                  </td>
                  <td className="px-3 py-2 text-lingxi-orange font-mono whitespace-nowrap">
                    {e.action}
                  </td>
                  <td className="px-3 py-2 text-white/80 whitespace-nowrap">
                    {e.actor || "-"}
                  </td>
                  <td className="px-3 py-2 text-white/70 font-mono whitespace-nowrap">
                    {e.target || "-"}
                  </td>
                  <td className="px-3 py-2 text-white/60 max-w-xs truncate">
                    {e.detail || "-"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
