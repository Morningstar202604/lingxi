"use client";

import { useCallback, useEffect, useState } from "react";
import {
  channelLabel,
  fetchSessions,
  statusLabel,
  summarizeSession,
  type SessionInfo,
} from "@/lib/api";
import ApiErrorPanel from "@/components/ApiState";

const statusColor: Record<string, string> = {
  intake: "text-blue-400",
  processing: "text-lingxi-orange",
  waiting_user: "text-yellow-400",
  escalated: "text-lingxi-red",
  resolved: "text-lingxi-green",
  closed: "text-gray-400",
};

function timeAgo(unixSec: number): string {
  const diff = Math.floor(Date.now() / 1000 - unixSec);
  if (diff < 60) return `${diff}s 前`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m 前`;
  return `${Math.floor(diff / 3600)}h 前`;
}

export default function SessionsPage() {
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [summaryTxt, setSummaryTxt] = useState<string | null>(null);
  const [summarizingId, setSummarizingId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await fetchSessions();
      setSessions(data);
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 3_000);
    return () => clearInterval(id);
  }, [load]);

  const onSummarize = async (s: SessionInfo) => {
    // 基于当前已知的消息概要构造一条消息 (后端 /api/sessions/summarize 支持任意消息数组)
    setSummarizingId(s.session_id);
    try {
      // 用 last_message 作为简化演示; 实际应用里可拉 /api/tickets/{id} 的完整 messages
      const summary = await summarizeSession([
        { role: s.last_role || "user", content: s.last_message || "(空)" },
        { role: "assistant", content: "(会话共 " + s.message_count + " 条消息)" },
      ]);
      setSummaryTxt(summary);
    } catch (e) {
      setSummaryTxt(`摘要生成失败: ${(e as Error).message}`);
    } finally {
      setSummarizingId(null);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="text-xs text-lingxi-muted">
          {sessions.length > 0
            ? `${sessions.length} 个活跃会话`
            : loading
              ? "加载中..."
              : "暂无活跃会话"}
        </div>
        <div className="w-2 h-2 rounded-full bg-lingxi-green pulse-dot" />
      </div>

      {summaryTxt && (
        <div className="card border-lingxi-orange/30 text-white/90 text-sm">
          <div className="flex items-center justify-between mb-1">
            <span className="text-xs text-lingxi-orange font-medium">会话摘要</span>
            <button
              onClick={() => setSummaryTxt(null)}
              className="text-xs text-lingxi-muted hover:text-white"
            >
              ✕
            </button>
          </div>
          <p className="whitespace-pre-wrap">{summaryTxt}</p>
        </div>
      )}

      {error ? (
        <ApiErrorPanel error={error} onRetry={load} compact />
      ) : sessions.length === 0 && !loading ? (
        <div className="card text-center py-12">
          <p className="text-sm text-lingxi-muted">暂无活跃会话</p>
          <p className="text-xs text-lingxi-muted/60 mt-1">
            当用户发送消息时会自动创建会话
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {sessions.map((s) => (
            <div key={s.session_id} className="card">
              <div className="flex items-center justify-between gap-3">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-xs font-mono text-lingxi-muted">
                      {s.session_id.slice(0, 10)}
                    </span>
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-white/5 border border-white/10 text-white/60">
                      {channelLabel(s.channel)}
                    </span>
                    <span
                      className={`text-xs font-medium ${
                        statusColor[s.status] || "text-gray-400"
                      }`}
                    >
                      {statusLabel(s.status)}
                    </span>
                    <span className="text-[10px] text-lingxi-muted">
                      {s.message_count} 条消息
                    </span>
                    <button
                      onClick={() => onSummarize(s)}
                      disabled={summarizingId === s.session_id}
                      className="text-[10px] px-1.5 py-0.5 rounded bg-lingxi-orange/10 border border-lingxi-orange/30 text-lingxi-orange hover:bg-lingxi-orange/20 transition-colors disabled:opacity-50"
                    >
                      {summarizingId === s.session_id ? "生成中…" : "生成摘要"}
                    </button>
                  </div>
                  {s.last_message && (
                    <p className="text-xs text-white/70 mt-1.5 truncate">
                      <span className="text-lingxi-muted">
                        {s.last_role === "user" ? "用户" : "Agent"}:
                      </span>{" "}
                      {s.last_message}
                    </p>
                  )}
                </div>
                <div className="shrink-0 text-[10px] text-lingxi-muted">
                  {timeAgo(s.last_activity)}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
