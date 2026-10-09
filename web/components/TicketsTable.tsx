"use client";

import { categoryLabel, channelLabel, statusLabel } from "@/lib/api";

interface TicketRow {
  ticket_id: string;
  channel: string;
  category: string;
  status: string;
  message_count: number;
}

interface Props {
  tickets: TicketRow[];
  compact?: boolean;
}

const statusColor: Record<string, string> = {
  intake: "bg-blue-500/10 text-blue-400 border-blue-500/20",
  processing: "bg-lingxi-orange/10 text-lingxi-orange border-lingxi-orange/20",
  waiting_user: "bg-yellow-500/10 text-yellow-400 border-yellow-500/20",
  escalated: "bg-lingxi-red/10 text-lingxi-red border-lingxi-red/20",
  resolved: "bg-lingxi-green/10 text-lingxi-green border-lingxi-green/20",
  closed: "bg-gray-500/10 text-gray-400 border-gray-500/20",
};

export default function TicketsTable({ tickets, compact = false }: Props) {
  if (tickets.length === 0) {
    return (
      <p className="text-xs text-lingxi-muted text-center py-8">
        暂无工单数据
      </p>
    );
  }

  return (
    <div className="overflow-x-auto -mx-2">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left border-b border-white/5 text-lingxi-muted">
            <th className="px-2 py-2 font-medium">ID</th>
            <th className="px-2 py-2 font-medium">渠道</th>
            <th className="px-2 py-2 font-medium">分类</th>
            <th className="px-2 py-2 font-medium">状态</th>
            {!compact && <th className="px-2 py-2 font-medium">消息数</th>}
          </tr>
        </thead>
        <tbody>
          {tickets.map((t) => (
            <tr
              key={t.ticket_id}
              className="border-b border-white/5 hover:bg-white/[0.02] transition-colors"
            >
              <td className="px-2 py-2 font-mono text-lingxi-muted">
                {t.ticket_id.slice(0, 8)}
              </td>
              <td className="px-2 py-2">{channelLabel(t.channel)}</td>
              <td className="px-2 py-2">{categoryLabel(t.category)}</td>
              <td className="px-2 py-2">
                <span
                  className={`px-1.5 py-0.5 rounded border text-[10px] ${
                    statusColor[t.status] || statusColor.closed
                  }`}
                >
                  {statusLabel(t.status)}
                </span>
              </td>
              {!compact && <td className="px-2 py-2 text-lingxi-muted">{t.message_count}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
