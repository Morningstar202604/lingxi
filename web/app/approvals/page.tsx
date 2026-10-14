"use client";

import { useCallback, useEffect, useState } from "react";
import {
  decideApproval,
  fetchApprovals,
  formatTime,
  type ApprovalItem,
  type ApprovalDecision,
} from "@/lib/api";
import ApiErrorPanel from "@/components/ApiState";

/**
 * 人工审批页。
 *
 * 对应 README「人工审批流」承诺的第三步：Agent 在 L2/L3 高风险调用点
 * interrupt 挂起后，坐席在这里看到待审批清单并决策 —— 决策会 resume
 * 挂起的图，用户侧立刻收到结果。此前只有后端端点，控制台无入口。
 */

const LEVEL_LABELS: Record<number, string> = {
  0: "L0 只读",
  1: "L1 低风险写",
  2: "L2 中风险",
  3: "L3 高风险",
  4: "L4 红线",
};

export default function ApprovalsPage() {
  const [items, setItems] = useState<ApprovalItem[]>([]);
  const [history, setHistory] = useState<ApprovalItem[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState("");
  const [decisions, setDecisions] = useState<Record<string, ApprovalDecision>>({});
  const [showHistory, setShowHistory] = useState(false);

  const load = useCallback(async () => {
    try {
      const pending = await fetchApprovals("pending");
      setItems(pending);
      setError(null);
    } catch (e) {
      setError(e);
    }
  }, []);

  const loadHistory = useCallback(async () => {
    try {
      const all = await fetchApprovals("all");
      setHistory(all.filter((i) => i.status !== "pending"));
    } catch (e) {
      setError(e);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 4_000);
    return () => clearInterval(id);
  }, [load]);

  useEffect(() => {
    if (showHistory) loadHistory();
  }, [showHistory, loadHistory]);

  const decide = useCallback(
    async (item: ApprovalItem, action: "approve" | "reject") => {
      setBusy(item.approval_id);
      try {
        const result = await decideApproval(item.approval_id, action, "dashboard-agent");
        setDecisions((prev) => ({ ...prev, [item.approval_id]: result }));
        // 决策后立刻刷新；挂起的图已 resume，列表可能已空
        await load();
      } catch (e) {
        setError(e);
      } finally {
        setBusy("");
      }
    },
    [load],
  );

  if (error && items.length === 0) {
    return <ApiErrorPanel error={error} onRetry={load} />;
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="text-xs text-lingxi-muted">
          {items.length > 0
            ? `${items.length} 项待审批 · 高风险操作挂起中`
            : "没有待审批项"}
        </div>
        <button
          onClick={() => setShowHistory((v) => !v)}
          className="px-3 py-1.5 rounded-lg text-xs border border-white/10 text-white/70 hover:bg-white/5 transition-colors"
        >
          {showHistory ? "隐藏历史" : "查看历史"}
        </button>
      </div>

      {items.length === 0 && (
        <div className="card text-xs text-lingxi-muted">
          Agent 触发 L2（需用户确认）/ L3（需管理员审批）操作时会挂起并出现在这里。
          L4 红线操作不进审批流 —— 无论谁批都不放行。
        </div>
      )}

      {items.map((item) => {
        const decision = decisions[item.approval_id];
        return (
          <div key={item.approval_id} className="card space-y-3">
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-sm font-semibold text-white">
                    {item.tool_name}
                  </span>
                  <span className="text-[10px] px-1.5 py-0.5 rounded border border-lingxi-orange/40 text-lingxi-orange">
                    {LEVEL_LABELS[item.required_level] ?? `L${item.required_level}`}
                  </span>
                  <span className="text-[10px] text-lingxi-muted">
                    请求方等级 L{item.requester_level}
                  </span>
                </div>
                <div className="text-xs text-lingxi-muted mt-1.5 space-x-3">
                  <span>会话 {item.chat_id}</span>
                  <span>用户 {item.user_id}</span>
                  <span>{item.channel}</span>
                  <span>{item.created_at ? formatTime(item.created_at) : ""}</span>
                </div>
              </div>
              <div className="shrink-0 flex gap-2">
                <button
                  onClick={() => decide(item, "approve")}
                  disabled={busy === item.approval_id}
                  className="px-4 py-2 rounded-lg text-xs border border-lingxi-green/30 bg-lingxi-green/10 text-lingxi-green hover:bg-lingxi-green/20 transition-colors disabled:opacity-40"
                >
                  {busy === item.approval_id ? "处理中..." : "批准"}
                </button>
                <button
                  onClick={() => decide(item, "reject")}
                  disabled={busy === item.approval_id}
                  className="px-4 py-2 rounded-lg text-xs border border-lingxi-red/30 bg-lingxi-red/10 text-lingxi-red hover:bg-lingxi-red/20 transition-colors disabled:opacity-40"
                >
                  拒绝
                </button>
              </div>
            </div>

            <div>
              <div className="text-[10px] text-lingxi-muted mb-1">调用参数</div>
              <pre className="p-3 rounded-lg bg-black/30 border border-white/5 text-[11px] text-white/80 overflow-x-auto">
                {JSON.stringify(item.arguments, null, 2)}
              </pre>
            </div>

            {decision && (
              <div
                className={`text-xs p-3 rounded-lg border ${
                  decision.status === "approved"
                    ? "border-lingxi-green/30 bg-lingxi-green/5 text-lingxi-green"
                    : "border-lingxi-red/30 bg-lingxi-red/5 text-lingxi-red"
                }`}
              >
                已{decision.status === "approved" ? "批准" : "拒绝"} ·
                {decision.resumed ? " 图已恢复执行" : " 未恢复（agent 不可用）"}
                {decision.final_reply && (
                  <div className="mt-1 text-white/80">回复：{decision.final_reply}</div>
                )}
                {decision.escalate_reason && (
                  <div className="mt-1 text-lingxi-muted">
                    升级原因：{decision.escalate_reason}
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}

      {showHistory && (
        <div className="space-y-2">
          <div className="text-xs text-lingxi-muted mt-4">历史决策</div>
          {history.length === 0 ? (
            <div className="card text-xs text-lingxi-muted">暂无历史</div>
          ) : (
            history.map((h) => (
              <div
                key={h.approval_id}
                className="card flex items-center justify-between text-xs"
              >
                <div className="space-x-3">
                  <span className="text-white/80">{h.tool_name}</span>
                  <span
                    className={
                      h.status === "approved"
                        ? "text-lingxi-green"
                        : h.status === "rejected"
                          ? "text-lingxi-red"
                          : "text-lingxi-muted"
                    }
                  >
                    {h.status === "approved" ? "已批准" : h.status === "rejected" ? "已拒绝" : "已过期"}
                  </span>
                  <span className="text-lingxi-muted">{h.chat_id}</span>
                </div>
                <span className="text-lingxi-muted">
                  {h.approver || "—"} · {h.decided_at ? formatTime(h.decided_at) : ""}
                </span>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
