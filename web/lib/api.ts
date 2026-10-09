"use client";

// ── Dashboard API client ──

const ADMIN_TOKEN =
  process.env.NEXT_PUBLIC_ADMIN_TOKEN || "change-me-in-production";
const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE || "";

function authHeaders(): HeadersInit {
  return {
    "x-admin-token": ADMIN_TOKEN,
    "Content-Type": "application/json",
  };
}

export interface DashboardSummary {
  active_tickets: number;
  escalated: number;
  processing: number;
  waiting_user: number;
  queue: {
    pending_count: number;
    claimed_count: number;
    longest_wait: number;
    top_5_pending: Array<{
      ticket_id: string;
      category: string;
      reason: string;
      wait_seconds: number;
    }>;
  };
}

export interface TicketSummary {
  ticket_id: string;
  chat_id: string;
  user_id: string;
  channel: string;
  category: string;
  status: string;
  message_count: number;
  created_at: number;
  updated_at: number;
  escalation_reason: string;
}

export interface QueueSnapshot {
  pending_count: number;
  claimed_count: number;
  longest_wait: number;
  top_5_pending: Array<{
    ticket_id: string;
    category: string;
    reason: string;
    wait_seconds: number;
  }>;
}

export async function fetchSummary(): Promise<DashboardSummary> {
  const res = await fetch(`${API_BASE}/api/dashboard/summary`, {
    headers: authHeaders(),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function fetchTickets(status?: string): Promise<TicketSummary[]> {
  const qs = status ? `?status=${status}` : "";
  const res = await fetch(`${API_BASE}/api/tickets${qs}`, {
    headers: authHeaders(),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function fetchQueue(): Promise<QueueSnapshot> {
  const res = await fetch(`${API_BASE}/api/queue`, {
    headers: authHeaders(),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function claimTicket(agentId: string): Promise<{
  ticket_id: string;
  chat_id: string;
  category: string;
  reason: string;
} | null> {
  const res = await fetch(
    `${API_BASE}/api/queue/claim?agent_id=${encodeURIComponent(agentId)}`,
    { method: "POST", headers: authHeaders() },
  );
  if (res.status === 204) return null;
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export function formatTime(unixSec: number): string {
  return new Date(unixSec * 1000).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function channelLabel(ch: string): string {
  const m: Record<string, string> = {
    qq_private: "QQ私聊",
    qq_group: "QQ群聊",
    wechat_private: "微信私聊",
    wechat_group: "微信群聊",
  };
  return m[ch] || ch;
}

export function categoryLabel(cat: string): string {
  const m: Record<string, string> = {
    inquiry: "咨询",
    complaint: "投诉",
    after_sales: "售后",
    other: "其他",
  };
  return m[cat] || cat;
}

export function statusLabel(s: string): string {
  const m: Record<string, string> = {
    intake: "接入中",
    processing: "处理中",
    waiting_user: "等用户回复",
    escalated: "已升级",
    resolved: "已解决",
    closed: "已关闭",
  };
  return m[s] || s;
}
