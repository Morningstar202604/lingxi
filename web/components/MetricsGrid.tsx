"use client";

import { useEffect, useRef, useState } from "react";

interface MetricCardProps {
  label: string;
  value: number | string;
  color?: "orange" | "green" | "red" | "navy" | "muted" | "blue" | "purple";
  sub?: string;
  trend?: { value: number; label: string; good: boolean };
  action?: { label: string; onClick: () => void; variant?: "primary" | "secondary" };
}

const colorMap: Record<string, string> = {
  orange: "text-lingxi-orange",
  green: "text-lingxi-green",
  red: "text-lingxi-red",
  navy: "text-blue-400",
  blue: "text-[#5B8DEF]",
  muted: "text-lingxi-muted",
  purple: "text-purple-400",
};

function useCountUp(target: number, durationMs = 600) {
  const [display, setDisplay] = useState(target);
  const fromRef = useRef(target);
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    const from = fromRef.current;
    if (from === target) return;
    const start = performance.now();
    const tick = (now: number) => {
      const p = Math.min(1, (now - start) / durationMs);
      const eased = 1 - Math.pow(1 - p, 3);
      const value = from + (target - from) * eased;
      setDisplay(value);
      fromRef.current = value;
      if (p < 1) {
        rafRef.current = requestAnimationFrame(tick);
      }
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
  }, [target, durationMs]);

  return display;
}

function formatValue(value: number | string): string {
  if (typeof value === "string") return value;
  return value.toLocaleString("zh-CN");
}

function MetricCard({ label, value, color = "navy", sub, trend, action }: MetricCardProps) {
  const isNumber = typeof value === "number";
  // Hook 必须无条件调用（react-hooks/rules-of-hooks）：非数字值传 0 让它空转，
  // 展示时再按类型区分。条件调用会让组件在 value 类型变化时崩掉。
  const animated = useCountUp(isNumber ? value : 0);
  const shown = isNumber
    ? Math.abs(animated - value) < 0.5
      ? value
      : animated
    : value;

  return (
    <div className="card transition-colors hover:border-lingxi-orange/30 relative overflow-hidden">
      <div className="absolute inset-0 bg-gradient-to-br from-lingxi-orange/5 to-transparent opacity-0 hover:opacity-100 transition-opacity" />
      <p className="text-xs text-lingxi-muted mb-1 relative z-10">{label}</p>
      <p className={`text-3xl font-bold tabular-nums ${colorMap[color]} relative z-10`}>
        {formatValue(shown)}
      </p>
      {sub && <p className="text-xs text-lingxi-muted mt-1 relative z-10">{sub}</p>}
      {trend && (
        <div className="absolute bottom-3 right-3 text-xs flex items-center gap-1">
          <span className={trend.good ? "text-lingxi-green" : "text-lingxi-red"}>
            {trend.good ? "▲" : "▼"} {Math.abs(trend.value).toFixed(1)}
          </span>
          <span className="text-lingxi-muted">{trend.label}</span>
        </div>
      )}
      {action && (
        <div className="absolute bottom-3 left-3">
          <button
            onClick={action.onClick}
            className={`text-xs px-2 py-1 rounded transition-colors ${
              action.variant === "primary"
                ? "bg-lingxi-green/10 text-lingxi-green hover:bg-lingxi-green/20 border border-lingxi-green/30"
                : "bg-white/5 text-lingxi-muted hover:bg-white/10 border border-white/10"
            }`}
          >
            {action.label}
          </button>
        </div>
      )}
    </div>
  );
}

interface Props {
  active: number;
  escalated: number;
  processing: number;
  waiting: number;
  resolved: number;
  resolutionRate: number;
  queuePending: number;
  longestWait: number;
  avgWait: number;
  slaBreachRate: number;
  slaCompliant: number;
  seatsOnline: number;
  seatsBusy: number;
  seatsUtilization: number;
  totalHandled: number;
  avgResponseSec: number;
}

export default function MetricsGrid(props: Props) {
  const {
    active,
    escalated,
    processing,
    waiting,
    resolved,
    resolutionRate,
    queuePending,
    longestWait,
    avgWait,
    slaBreachRate,
    slaCompliant,
    seatsOnline,
    seatsBusy,
    seatsUtilization,
    totalHandled,
    avgResponseSec,
  } = props;

  return (
    <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6 gap-3">
      {/* Row 1: Core ticket metrics */}
      <MetricCard
        label="活跃工单"
        value={active}
        color="blue"
        sub="当前处理中"
      />
      <MetricCard
        label="处理中"
        value={processing}
        color="orange"
        sub="Agent 正在跟进"
      />
      <MetricCard
        label="等用户回复"
        value={waiting}
        color="muted"
        sub="需用户提供信息"
      />
      <MetricCard
        label="已升级"
        value={escalated}
        color="red"
        sub="等待人工接管"
      />
      <MetricCard
        label="已解决"
        value={resolved}
        color="green"
        sub={`解决率 ${(resolutionRate * 100).toFixed(1)}%`}
      />
      <MetricCard
        label="队列待分配"
        value={queuePending}
        color="orange"
        sub={`最长等待 ${longestWait}s`}
      />

      {/* Row 2: SLA & Quality */}
      <MetricCard
        label="SLA 合规"
        value={slaCompliant}
        color={slaBreachRate > 0.1 ? "red" : "green"}
        sub={`违规率 ${(slaBreachRate * 100).toFixed(1)}%`}
      />
      <MetricCard
        label="平均等待"
        value={avgWait}
        color={avgWait > 120 ? "red" : avgWait > 60 ? "orange" : "green"}
        sub="秒 / 升级队列"
      />
      <MetricCard
        label="最长等待"
        value={longestWait}
        color={longestWait > 300 ? "red" : "orange"}
        sub="需关注的工单"
      />

      {/* Row 3: Agent Capacity */}
      <MetricCard
        label="在线坐席"
        value={seatsOnline}
        color="blue"
        sub={`忙碌 ${seatsBusy} · 空闲 ${seatsOnline - seatsBusy}`}
      />
      <MetricCard
        label="坐席利用率"
        value={`${(seatsUtilization * 100).toFixed(1)}%`}
        color={seatsUtilization > 0.85 ? "red" : seatsUtilization > 0.6 ? "orange" : "green"}
        sub={`容量 ${seatsBusy > 0 ? "已用" : "空闲"}`}
      />
      <MetricCard
        label="累计处理"
        value={totalHandled}
        color="purple"
        sub="本会话内"
      />
      <MetricCard
        label="平均响应"
        value={avgResponseSec}
        color={avgResponseSec > 120 ? "orange" : "green"}
        sub="秒 / 首次响应"
      />
    </div>
  );
}