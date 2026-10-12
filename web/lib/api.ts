"use client";

// ── Dashboard API client ──

// 安全：NEXT_PUBLIC_* 会被编译进客户端 bundle 下发给每个访客，
// 绝不能内置管理凭据默认值。凭据必须由运维在登录后注入。
const ADMIN_TOKEN = process.env.NEXT_PUBLIC_ADMIN_TOKEN || "";
// Dashboard 全部经 Next rewrite 代理访问（同源，无需 CORS）。
// 后端地址在 next.config.js 由 DASHBOARD_API_URL 配置。
const API_BASE = "/api/proxy";

function authHeaders(): HeadersInit {
  if (!ADMIN_TOKEN) {
    throw new Error(
      "未配置 NEXT_PUBLIC_ADMIN_TOKEN：请在 .env.local 设置后重启前端",
    );
  }
  // 不带 Content-Type：GET 请求加 application/json 会触发 CORS 预检
  return { "x-admin-token": ADMIN_TOKEN };
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

export interface SessionInfo {
  session_id: string;
  chat_id: string;
  user_id: string;
  channel: string;
  status: string;
  message_count: number;
  last_activity: number;
  last_message: string;
  last_role: string;
}

export interface LogEntry {
  time: string;
  level: string;
  logger: string;
  msg: string;
  trace_id?: string;
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
  // 后端空队列返回 404（REST 语义），前端映射为 null 表示"无可认领"
  if (res.status === 204 || res.status === 404) return null;
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

export async function fetchSessions(): Promise<SessionInfo[]> {
  const res = await fetch(`${API_BASE}/api/sessions`, {
    headers: authHeaders(),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function fetchLogs(limit = 200): Promise<LogEntry[]> {
  const res = await fetch(`${API_BASE}/api/logs?limit=${limit}`, {
    headers: authHeaders(),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export interface CompleteResult {
  ticket_id: string;
  status: string;
}

export async function completeTicket(ticketId: string): Promise<CompleteResult> {
  const res = await fetch(
    `${API_BASE}/api/queue/complete?ticket_id=${encodeURIComponent(ticketId)}`,
    { method: "POST", headers: authHeaders() },
  );
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

// ── Knowledge 知识库 (RAG) ──

export interface KnowledgeStats {
  name: string;
  count: number;
}

export interface KnowledgeDoc {
  doc_id: string;
  source: string;
}

export interface RecallHit {
  text: string;
  source: string;
  chunk_index: number;
  doc_id: string;
  score: number;
}

export async function fetchKnowledgeStats(): Promise<KnowledgeStats> {
  const res = await fetch(`${API_BASE}/api/knowledge/stats`, {
    headers: authHeaders(),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function fetchKnowledgeDocs(): Promise<KnowledgeDoc[]> {
  const res = await fetch(`${API_BASE}/api/knowledge/docs`, {
    headers: authHeaders(),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function uploadKnowledge(
  content: string,
  docId = "",
  source = "",
): Promise<{ doc_id: string; chunk_count: number; source: string }> {
  const res = await fetch(`${API_BASE}/api/knowledge/upload`, {
    method: "POST",
    headers: { ...authHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({ content, doc_id: docId, source }),
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function recallKnowledge(
  query: string,
  topK = 5,
): Promise<RecallHit[]> {
  const res = await fetch(`${API_BASE}/api/knowledge/recall`, {
    method: "POST",
    headers: { ...authHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({ query, top_k: topK }),
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function deleteKnowledgeDoc(docId: string): Promise<{ deleted_chunks: number }> {
  const res = await fetch(
    `${API_BASE}/api/knowledge/docs/${encodeURIComponent(docId)}`,
    { method: "DELETE", headers: authHeaders() },
  );
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function resetKnowledge(): Promise<{ status: string }> {
  const res = await fetch(`${API_BASE}/api/knowledge/reset`, {
    method: "POST",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

// ── Phase 3: 审计日志 (Audit Log) ──

export interface AuditEntry {
  action: string;
  actor: string;
  target: string;
  detail: string;
  timestamp: number;
}

export async function fetchAuditLogs(
  action = "",
  actor = "",
  limit = 200,
): Promise<AuditEntry[]> {
  const params = new URLSearchParams();
  if (action) params.set("action", action);
  if (actor) params.set("actor", actor);
  params.set("limit", String(limit));
  const qs = params.toString();
  const res = await fetch(`${API_BASE}/api/audit${qs ? `?${qs}` : ""}`, {
    headers: authHeaders(),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function exportAudit(): Promise<void> {
  const res = await fetch(`${API_BASE}/api/audit/export`, {
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  // 触发浏览器下载
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "audit_log.json";
  document.body.appendChild(a);
  a.click();
  // 立刻清理短链接与临时节点，不污染 DOM
  URL.revokeObjectURL(url);
  a.remove();
}

// ── Phase 2: 会话摘要 ──

export async function summarizeSession(
  messages: Array<{ role: string; content: string }>,
): Promise<string> {
  const res = await fetch(`${API_BASE}/api/sessions/summarize`, {
    method: "POST",
    headers: { ...authHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({ messages }),
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  const data = await res.json();
  return data.summary as string;
}

// ── 实验管理 (A/B Testing) ──

export interface ExperimentVariant {
  name: string;
  variant_type: string;
  weight: number;
  persona_overrides: Record<string, unknown>;
  sample_size?: number;
  resolution_rate?: number;
  escalation_rate?: number;
  avg_duration_seconds?: number;
  total_messages?: number;
}

export interface Experiment {
  name: string;
  description: string;
  status: string;
  variants: ExperimentVariant[];
  target_metric: string;
  min_sample_size: number;
  created_at: number;
  tenant_id: string;
}

export interface ExperimentDetail extends Experiment {
  winner_analysis: {
    status: string;
    winner?: string;
    confidence?: number;
    recommended_action?: string;
    baseline?: Record<string, unknown>;
    challenger?: Record<string, unknown>;
  };
  total_assignments: number;
}

export async function fetchExperiments(): Promise<Experiment[]> {
  const res = await fetch(`${API_BASE}/api/experiments`, {
    headers: authHeaders(),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function fetchExperiment(name: string): Promise<ExperimentDetail> {
  const res = await fetch(`${API_BASE}/api/experiments/${encodeURIComponent(name)}`, {
    headers: authHeaders(),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function createExperiment(config: {
  name: string;
  description?: string;
  variants: Array<{
    name: string;
    variant_type: string;
    weight: number;
    persona_overrides?: Record<string, unknown>;
  }>;
  target_metric?: string;
  min_sample_size?: number;
}): Promise<{ name: string; status: string }> {
  const res = await fetch(`${API_BASE}/api/experiments`, {
    method: "POST",
    headers: { ...authHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function updateExperimentStatus(
  name: string,
  status: string,
): Promise<{ name: string; status: string }> {
  const res = await fetch(`${API_BASE}/api/experiments/${encodeURIComponent(name)}/status`, {
    method: "PUT",
    headers: { ...authHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function deleteExperiment(name: string): Promise<{ deleted: string }> {
  const res = await fetch(`${API_BASE}/api/experiments/${encodeURIComponent(name)}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function recordExperimentMetric(
  name: string,
  variantName: string,
  userId: string,
  resolved: boolean,
  escalated: boolean = false,
): Promise<{ recorded: boolean }> {
  const res = await fetch(`${API_BASE}/api/experiments/${encodeURIComponent(name)}/metrics`, {
    method: "POST",
    headers: { ...authHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({
      variant_name: variantName,
      user_id: userId,
      resolved,
      escalated,
    }),
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

// ── SSE 事件流（fetch + ReadableStream）──
// 不用 EventSource：它不支持自定义请求头，而 /api/events 要求 x-admin-token。
// 把 token 塞进 query 会进浏览器历史、Nginx 访问日志与 Referer —— 那是降级
// 安全。这里自己解 SSE 帧，代价是 ~40 行，换来凭据只走请求头。
export interface SSEEvent {
  event: string;
  data: unknown;
}

export type SSEHandler = (ev: SSEEvent) => void;

/**
 * 订阅后端 SSE。返回 close() 用于取消订阅。
 *
 * 解析规则按 W3C EventSource：事件以空行分隔，data: 多行拼接，
 * 注释行（以 : 开头）忽略。
 */
export async function subscribeSSE(
  path: string,
  onEvent: SSEHandler,
  opts: { signal?: AbortSignal; method?: string; body?: unknown } = {},
): Promise<() => void> {
  const controller = new AbortController();
  if (opts.signal) {
    opts.signal.addEventListener("abort", () => controller.abort(), { once: true });
  }

  const res = await fetch(`${API_BASE}${path}`, {
    method: opts.method ?? "GET",
    headers: {
      ...authHeaders(),
      ...(opts.body ? { "Content-Type": "application/json" } : {}),
    },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    signal: controller.signal,
    cache: "no-store",
  });

  if (!res.ok || !res.body) {
    throw new Error(`SSE 连接失败: ${res.status}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  void (async () => {
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // 按空行切帧（兼容 \n\n 与 \r\n\r\n）
        for (;;) {
          const sep = /\r?\n\r?\n/.exec(buffer);
          if (!sep) break;
          const frame = buffer.slice(0, sep.index);
          buffer = buffer.slice(sep.index + sep[0].length);

          let event = "message";
          const dataLines: string[] = [];
          for (const line of frame.split(/\r?\n/)) {
            if (!line || line.startsWith(":")) continue;
            if (line.startsWith("event:")) event = line.slice(6).trim();
            else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
          }
          if (!dataLines.length) continue;
          const raw = dataLines.join("\n");
          let parsed: unknown = raw;
          try {
            parsed = JSON.parse(raw);
          } catch {
            /* 非 JSON 原样透传 */
          }
          onEvent({ event, data: parsed });
        }
      }
    } catch {
      // 取消订阅或连接断开：静默结束，交由调用方处理重连
    }
  })();

  return () => controller.abort();
}

/** 订阅升级/认领/关闭事件流 */
export function subscribeQueueEvents(
  onEvent: SSEHandler,
  opts: { signal?: AbortSignal } = {},
): Promise<() => void> {
  return subscribeSSE("/api/events", onEvent, opts);
}

// ── 人格配置 (Persona 13 模块) ──

export interface PersonaIdentity {
  name: string;
  role: string;
  backstory: string;
  avatar_url: string | null;
  speaking_style: string;
  signature: string;
}

export interface KeywordRule {
  keywords: string[];
  action: string;
  payload: string;
  priority: number;
}

export interface PersonaState {
  identity: PersonaIdentity;
  behavior: Record<string, unknown>;
  modules: Record<string, boolean>;
  active_modules: string[];
  keyword_rules: KeywordRule[];
  custom_prompt_keys: string[];
}

export interface PersonaPreview {
  system_prompt: string;
  chars: number;
  guardrail: { verdict: string; pattern: string | null };
}

export async function fetchPersona(): Promise<PersonaState> {
  const res = await fetch(`${API_BASE}/api/persona`, {
    headers: authHeaders(),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function previewPersona(
  message: string,
  memoryBlock = "",
  lorebookBlock = "",
): Promise<PersonaPreview> {
  const res = await fetch(`${API_BASE}/api/persona/preview`, {
    method: "POST",
    headers: { ...authHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({
      message,
      memory_block: memoryBlock,
      lorebook_block: lorebookBlock,
    }),
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}
