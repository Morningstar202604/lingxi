"use client";

import { useEffect, useRef, useState } from "react";

interface MetricCardProps {
  label: string;
  value: number;
  color?: "orange" | "green" | "red" | "navy" | "muted" | "blue";
  sub?: string;
}

const colorMap: Record<string, string> = {
  orange: "text-lingxi-orange",
  green: "text-lingxi-green",
  red: "text-lingxi-red",
  navy: "text-blue-400",
  blue: "text-[#5B8DEF]",
  muted: "text-lingxi-muted",
};

/**
 * 数值滚动动画：从上一次的值过渡到当前值，而不是每 5 秒硬跳一次。
 * 用 rAF + easeOutCubic，避免轮询刷新造成的视觉抖动。
 */
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

function MetricCard({ label, value, color = "navy", sub }: MetricCardProps) {
  const animated = useCountUp(value);
  const shown = Math.abs(animated - value) < 0.5 ? value : animated;
  return (
    <div className="card transition-colors hover:border-lingxi-orange/30">
      <p className="text-xs text-lingxi-muted mb-1">{label}</p>
      <p className={`text-3xl font-bold tabular-nums ${colorMap[color]}`}>
        {shown.toLocaleString("zh-CN")}
      </p>
      {sub && <p className="text-xs text-lingxi-muted mt-1">{sub}</p>}
    </div>
  );
}

interface Props {
  active: number;
  escalated: number;
  processing: number;
  waiting: number;
  queuePending: number;
  longestWait: number;
}

export default function MetricsGrid({
  active,
  escalated,
  processing,
  waiting,
  queuePending,
  longestWait,
}: Props) {
  return (
    <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
      <MetricCard label="活跃工单" value={active} color="blue" />
      <MetricCard label="处理中" value={processing} color="orange" />
      <MetricCard label="等用户" value={waiting} color="muted" />
      <MetricCard label="已升级" value={escalated} color="red" />
      <MetricCard label="队列待分配" value={queuePending} color="orange" />
      <MetricCard
        label="最长等待(秒)"
        value={Math.round(longestWait)}
        color={longestWait > 120 ? "red" : "green"}
      />
    </div>
  );
}