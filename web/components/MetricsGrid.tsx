"use client";

interface MetricCardProps {
  label: string;
  value: number;
  color?: "orange" | "green" | "red" | "navy" | "muted";
  sub?: string;
}

const colorMap: Record<string, string> = {
  orange: "text-lingxi-orange",
  green: "text-lingxi-green",
  red: "text-lingxi-red",
  navy: "text-blue-400",
  muted: "text-lingxi-muted",
};

function MetricCard({ label, value, color = "navy", sub }: MetricCardProps) {
  return (
    <div className="card">
      <p className="text-xs text-lingxi-muted mb-1">{label}</p>
      <p className={`text-3xl font-bold ${colorMap[color]}`}>{value}</p>
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
      <MetricCard label="活跃工单" value={active} color="navy" />
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
