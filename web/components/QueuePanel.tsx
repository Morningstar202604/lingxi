"use client";

import { categoryLabel } from "@/lib/api";

interface QueueItem {
  ticket_id: string;
  category: string;
  reason: string;
  wait_seconds: number;
}

interface Props {
  items: QueueItem[];
  onClaim?: (ticketId: string) => void;
}

export default function QueuePanel({ items, onClaim }: Props) {
  if (items.length === 0) {
    return (
      <div className="card">
        <h3 className="text-sm font-medium text-white/80 mb-3">升级队列</h3>
        <p className="text-xs text-lingxi-muted">当前无待分配的升级工单</p>
      </div>
    );
  }

  return (
    <div className="card">
      <h3 className="text-sm font-medium text-white/80 mb-3">
        升级队列{" "}
        <span className="text-xs text-lingxi-muted font-normal">
          ({items.length} 待分配)
        </span>
      </h3>
      <div className="space-y-2 max-h-64 overflow-y-auto">
        {items.map((item) => (
          <div
            key={item.ticket_id}
            className="flex items-center justify-between gap-3 px-3 py-2 bg-white/5 rounded-lg"
          >
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-xs font-mono text-lingxi-muted truncate">
                  {item.ticket_id.slice(0, 10)}
                </span>
                <span className="text-xs px-1.5 py-0.5 rounded bg-lingxi-orange/10 text-lingxi-orange border border-lingxi-orange/20">
                  {categoryLabel(item.category)}
                </span>
              </div>
              <p className="text-xs text-lingxi-muted mt-0.5 truncate">
                原因：{item.reason}
              </p>
            </div>
            <div className="flex items-center gap-3 shrink-0">
              <span className="text-xs text-lingxi-muted">
                {item.wait_seconds}s
              </span>
              {onClaim && (
                <button
                  onClick={() => onClaim(item.ticket_id)}
                  className="text-xs px-2 py-1 rounded bg-lingxi-green/10 text-lingxi-green hover:bg-lingxi-green/20 border border-lingxi-green/30 transition-colors"
                >
                  认领
                </button>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
