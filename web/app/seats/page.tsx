"use client";

import { useCallback, useEffect, useState } from "react";
import {
  fetchSeatMetrics,
  fetchSeats,
  registerSeat,
  updateSeatStatus,
  type Seat,
  type SeatMetrics,
} from "@/lib/api";
import ApiErrorPanel from "@/components/ApiState";

/**
 * 坐席管理页（seat/router.py 的 5 个端点此前控制台零消费）。
 *
 * 升级工单会经消息链路自动尝试分派；这里负责坐席的注册、
 * 上下线/状态切换，以及容量与利用率观测。
 */

const STATUS_LABELS: Record<string, string> = {
  offline: "离线",
  idle: "空闲",
  busy: "忙碌",
  break: "休息",
};

const STATUS_COLORS: Record<string, string> = {
  offline: "bg-gray-500/20 text-gray-300",
  idle: "bg-lingxi-green/20 text-lingxi-green",
  busy: "bg-lingxi-orange/20 text-lingxi-orange",
  break: "bg-yellow-500/20 text-yellow-300",
};

const NEXT_STATUS: Record<string, string> = {
  offline: "idle",
  idle: "busy",
  busy: "break",
  break: "idle",
};

export default function SeatsPage() {
  const [seats, setSeats] = useState<Seat[]>([]);
  const [metrics, setMetrics] = useState<SeatMetrics | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState("");
  const [form, setForm] = useState({ username: "", displayName: "", maxConcurrent: 3 });

  const load = useCallback(async () => {
    try {
      const [list, m] = await Promise.all([fetchSeats(), fetchSeatMetrics()]);
      setSeats(list);
      setMetrics(m);
      setError(null);
    } catch (e) {
      setError(e);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 8_000);
    return () => clearInterval(id);
  }, [load]);

  const handleRegister = useCallback(async () => {
    if (!form.username.trim()) return;
    setBusy("register");
    try {
      await registerSeat(form.username.trim(), form.displayName.trim(), form.maxConcurrent);
      setForm({ username: "", displayName: "", maxConcurrent: 3 });
      await load();
    } catch (e) {
      setError(e);
    } finally {
      setBusy("");
    }
  }, [form, load]);

  const cycleStatus = useCallback(
    async (seat: Seat) => {
      const next = NEXT_STATUS[seat.status] ?? "idle";
      setBusy(seat.username);
      try {
        await updateSeatStatus(seat.username, next);
        await load();
      } catch (e) {
        setError(e);
      } finally {
        setBusy("");
      }
    },
    [load],
  );

  if (error && seats.length === 0) {
    return <ApiErrorPanel error={error} onRetry={load} />;
  }

  return (
    <div className="space-y-6">
      {/* ── 利用率概览 ── */}
      {metrics && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div className="card">
            <div className="text-xs text-lingxi-muted">在线坐席</div>
            <div className="text-2xl font-bold text-white mt-1">
              {metrics.online_seats}
              <span className="text-sm text-lingxi-muted font-normal">
                {" "}
                / {metrics.total_seats}
              </span>
            </div>
            <div className="text-xs text-lingxi-muted mt-1">
              忙碌 {metrics.busy_seats} · 空闲 {metrics.idle_seats}
            </div>
          </div>
          <div className="card">
            <div className="text-xs text-lingxi-muted">容量利用率</div>
            <div className="text-2xl font-bold text-white mt-1">
              {(metrics.utilization_rate * 100).toFixed(1)}%
            </div>
            <div className="text-xs text-lingxi-muted mt-1">
              {metrics.used_capacity} / {metrics.total_capacity} 并发位
            </div>
          </div>
          <div className="card">
            <div className="text-xs text-lingxi-muted">累计处理</div>
            <div className="text-2xl font-bold text-white mt-1">
              {metrics.total_handled}
            </div>
            <div className="text-xs text-lingxi-muted mt-1">本会话内</div>
          </div>
          <div className="card">
            <div className="text-xs text-lingxi-muted">自动分派</div>
            <div className="text-2xl font-bold text-lingxi-green mt-1">已启用</div>
            <div className="text-xs text-lingxi-muted mt-1">工单升级即尝试分配</div>
          </div>
        </div>
      )}

      {/* ── 注册坐席 ── */}
      <div className="card">
        <div className="text-sm font-semibold text-white mb-3">注册坐席</div>
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <div className="text-[10px] text-lingxi-muted mb-1">用户名</div>
            <input
              value={form.username}
              onChange={(e) => setForm({ ...form, username: e.target.value })}
              placeholder="alice"
              className="px-3 py-2 rounded-lg bg-black/30 border border-white/10 text-xs text-white focus:border-lingxi-orange/50 focus:outline-none"
            />
          </div>
          <div>
            <div className="text-[10px] text-lingxi-muted mb-1">显示名（可选）</div>
            <input
              value={form.displayName}
              onChange={(e) => setForm({ ...form, displayName: e.target.value })}
              placeholder="小王"
              className="px-3 py-2 rounded-lg bg-black/30 border border-white/10 text-xs text-white focus:border-lingxi-orange/50 focus:outline-none"
            />
          </div>
          <div>
            <div className="text-[10px] text-lingxi-muted mb-1">并发上限</div>
            <input
              type="number"
              min={1}
              max={20}
              value={form.maxConcurrent}
              onChange={(e) =>
                setForm({ ...form, maxConcurrent: Number(e.target.value) || 1 })
              }
              className="w-20 px-3 py-2 rounded-lg bg-black/30 border border-white/10 text-xs text-white focus:border-lingxi-orange/50 focus:outline-none"
            />
          </div>
          <button
            onClick={handleRegister}
            disabled={busy === "register" || !form.username.trim()}
            className="px-4 py-2 rounded-lg text-xs border border-lingxi-green/30 bg-lingxi-green/10 text-lingxi-green hover:bg-lingxi-green/20 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {busy === "register" ? "注册中..." : "注册"}
          </button>
        </div>
      </div>

      {/* ── 坐席列表 ── */}
      <div className="card overflow-x-auto">
        <div className="text-sm font-semibold text-white mb-3">
          坐席列表（{seats.length}）
        </div>
        {seats.length === 0 ? (
          <div className="text-xs text-lingxi-muted">
            尚无坐席。注册后工单升级时会自动尝试分派；也可用队列页手动认领。
          </div>
        ) : (
          <div className="table-scroll">
          <table className="w-full text-xs min-w-[720px]">
            <thead>
              <tr className="text-lingxi-muted border-b border-white/5">
                <th className="text-left py-2 pr-4 font-normal">坐席</th>
                <th className="text-left py-2 pr-4 font-normal">状态</th>
                <th className="text-left py-2 pr-4 font-normal">负载</th>
                <th className="text-left py-2 pr-4 font-normal">累计处理</th>
                <th className="text-left py-2 pr-4 font-normal">平均响应</th>
                <th className="text-right py-2 font-normal">操作</th>
              </tr>
            </thead>
            <tbody>
              {seats.map((s) => (
                <tr key={s.username} className="border-b border-white/5 last:border-0">
                  <td className="py-2.5 pr-4">
                    <div className="text-white/90">{s.display_name || s.username}</div>
                    <div className="text-[10px] text-lingxi-muted">{s.username}</div>
                  </td>
                  <td className="py-2.5 pr-4">
                    <span
                      className={`text-[10px] px-1.5 py-0.5 rounded ${STATUS_COLORS[s.status] ?? ""}`}
                    >
                      {STATUS_LABELS[s.status] ?? s.status}
                    </span>
                  </td>
                  <td className="py-2.5 pr-4 text-white/70">
                    {s.current_ticket_count} / {s.max_concurrent}
                  </td>
                  <td className="py-2.5 pr-4 text-white/70">{s.total_handled}</td>
                  <td className="py-2.5 pr-4 text-white/70">
                    {s.avg_response_seconds > 0
                      ? `${s.avg_response_seconds.toFixed(0)}s`
                      : "—"}
                  </td>
                  <td className="py-2.5 text-right">
                    <button
                      onClick={() => cycleStatus(s)}
                      disabled={busy === s.username}
                      className="px-3 py-1.5 rounded-lg text-[11px] border border-white/10 text-white/70 hover:bg-white/5 transition-colors disabled:opacity-40"
                    >
                      {busy === s.username
                        ? "..."
                        : `切到${STATUS_LABELS[NEXT_STATUS[s.status] ?? "idle"]}`}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </div>
    </div>
  );
}
