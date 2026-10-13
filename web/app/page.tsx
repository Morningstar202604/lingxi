"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  StatusDonut,
  TrendChart,
  WaitGauge,
  type StatusSlice,
  type TrendPoint,
} from "@/components/DashboardCharts";
import EventStreamPanel from "@/components/EventStreamPanel";
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
import ApiErrorPanel from "@/components/ApiState";

/** 趋势线保留的采样点数：5s/次 × 60 ≈ 5 分钟 */
const TREND_WINDOW = 60;

export default function DashboardPage() {
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [queue, setQueue] = useState<QueueSnapshot | null>(null);
  const [recent, setRecent] = useState<TicketSummary[]>([]);
  const [trend, setTrend] = useState<TrendPoint[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [lastSync, setLastSync] = useState<number | null>(null);
  const trendRef = useRef<TrendPoint[]>([]);

  const load = useCallback(async () => {
    try {
      const [s, q, t] = await Promise.all([
        fetchSummary(),
        fetchQueue(),
        fetchTickets(),
      ]);
      setSummary(s);
      setQueue(q);
      // 后端按 dict 插入序返回，slice(0,10) 取到的是**最早** 10 条；
      // 页面标题写"最近活跃"，所以必须自己按更新时间倒序（见 AUDIT-2026-10-10.md #27）
      setRecent(
        [...t].sort((a, b) => b.updated_at - a.updated_at).slice(0, 10),
      );
      const next: TrendPoint[] = [
        ...trendRef.current,
        {
          t: Date.now(),
          active: s.active_tickets,
          escalated: s.escalated,
          queuePending: s.queue.pending_count,
        },
      ].slice(-TREND_WINDOW);
      trendRef.current = next;
      setTrend(next);
      setLastSync(Date.now());
      setError(null);
    } catch (e) {
      // 不清空已有数据：偶发一次抖动不该把整个面板换成错误页
      setError(e);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 5_000);
    return () => clearInterval(id);
  }, [load]);

  const slices: StatusSlice[] = summary
    ? [
        { name: "处理中", value: summary.processing, color: "#E8963E" },
        { name: "已升级", value: summary.escalated, color: "#E74C3C" },
        { name: "等用户", value: summary.waiting_user, color: "#5B8DEF" },
        {
          name: "接入中",
          value: Math.max(
            0,
            summary.active_tickets -
              summary.processing -
              summary.escalated -
              summary.waiting_user,
          ),
          color: "#95A5A6",
        },
      ]
    : [];

  // 曾经这里是整页 early-return：一没配凭据就把 Dashboard 换成一块红牌。
  // 现在改成**非阻断横幅**——真实数据（若有）照常渲染，只有缺失的那部分提示原因。
  return (
    <div className="space-y-6">
      {Boolean(error) && !summary && (
        <ApiErrorPanel error={error} onRetry={load} />
      )}
      {Boolean(error) && summary && (
        <div className="flex items-center justify-between gap-3 rounded-lg border border-lingxi-orange/30 bg-lingxi-orange/[0.06] px-3 py-2">
          <span className="text-xs text-lingxi-orange">
            最近一次同步失败：显示的是上一次成功的数据
          </span>
          <button
            onClick={load}
            className="text-xs px-2 py-1 rounded border border-lingxi-orange/30 text-lingxi-orange hover:bg-lingxi-orange/10 transition-colors"
          >
            重试
          </button>
        </div>
      )}

      {/* 实时指标 */}
      <section>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-xs font-medium text-lingxi-muted uppercase tracking-wider">
            实时指标
          </h3>
          <span className="flex items-center gap-1.5 text-[10px] text-lingxi-muted">
            <span className="w-1.5 h-1.5 rounded-full bg-lingxi-green pulse-dot" />
            {lastSync
              ? `同步于 ${new Date(lastSync).toLocaleTimeString("zh-CN")}`
              : "连接中…"}
          </span>
        </div>
        {summary ? (
          <MetricsGrid
            active={summary.active_tickets}
            escalated={summary.escalated}
            processing={summary.processing}
            waiting={summary.waiting_user}
            resolved={summary.resolved ?? 0}
            resolutionRate={summary.resolution_rate ?? 0}
            queuePending={summary.queue.pending_count}
            longestWait={summary.queue.longest_wait}
            avgWait={summary.avg_wait_seconds ?? 0}
            slaBreachRate={summary.sla?.breach_rate ?? 0}
            slaCompliant={summary.sla?.compliant ?? 0}
            seatsOnline={summary.seats?.online_seats ?? 0}
            seatsBusy={summary.seats?.busy_seats ?? 0}
            seatsUtilization={summary.seats?.utilization_rate ?? 0}
            totalHandled={summary.seats?.total_handled ?? 0}
            avgResponseSec={summary.seats?.avg_response_seconds ?? 0}
          />
        ) : (
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="card animate-pulse h-20" />
            ))}
          </div>
        )}
      </section>

      {/* 动态可视化：趋势 / 状态构成 / 等待时长 / 实时事件 */}
      <section>
        <h3 className="text-xs font-medium text-lingxi-muted uppercase tracking-wider mb-3">
          可视化分析
        </h3>
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div className="lg:col-span-1">
            <StatusDonut slices={slices} />
          </div>
          <div className="lg:col-span-1">
            <WaitGauge
              seconds={summary?.queue.longest_wait ?? 0}
              threshold={120}
            />
          </div>
          <div className="lg:col-span-1">
            <TrendChart points={trend} />
          </div>
        </div>
      </section>

      {/* 实时事件流（SSE） */}
      <section>
        <h3 className="text-xs font-medium text-lingxi-muted uppercase tracking-wider mb-3">
          实时事件
        </h3>
        <EventStreamPanel />
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