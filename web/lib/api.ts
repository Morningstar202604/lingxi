"use client";

// ── Dashboard API client ──

// 安全：NEXT_PUBLIC_* 会被编译进客户端 bundle 下发给每个访客，
// 绝不能内置管理凭据默认值。凭据必须由运维在登录后注入。
const ADMIN_TOKEN = process.env.NEXT_PUBLIC_ADMIN_TOKEN || "";

// 前端**直连**后端，不走 Next rewrites 代理。
// 原因：/api/events 是 SSE 长连接，Next dev 的 rewrites 会缓冲响应体 ——
// 浏览器拿到 200 text/event-stream 却一帧都收不到（实测复现）。
// 代理只对普通请求友好，对流式是死路；后端已开 loopback CORS（见
// flow/dashboard_api.py::_install_cors），dev 与 Tauri 因此行为一致。
//
// 需要跨机访问时用 NEXT_PUBLIC_API_BASE 覆盖，例如 http://10.0.0.5:8080，
// 同时在服务端 security.cors_origins 里放行该 origin。
export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE || "http://127.0.0.1:8080";

/** 是否已注入管理凭据（仅用于界面提示，不阻断请求）。 */
export function hasAdminToken(): boolean {
  return Boolean(ADMIN_TOKEN);
}

function authHeaders(): HeadersInit {
  // ⚠️ 刻意**不抛错**。此前这里 throw，导致 useEffect 里的 fetch 全部失败，
  // 页面直接渲染"无法连接后端"整屏错误 —— 而后端其实好好的，只是没配凭据。
  // 正确做法：照常发请求（不带凭据），让后端返回权威的 503/401 与处置建议，
  // 再由 <ApiState> 把那句建议原样呈现给用户。
  // 带错 token 会被后端 401，不会静默放行，所以这里省掉 header 是安全的。
  // 不带 Content-Type：GET 请求加 application/json 会触发 CORS 预检。
  return ADMIN_TOKEN ? { "x-admin-token": ADMIN_TOKEN } : {};
}

/** 结构化 API 错误：带状态码、后端 detail 与可执行建议。 */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly hint: string;

  constructor(status: number, detail: string, hint = "") {
    super(detail || `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.hint = hint;
  }

  /** 是不是"没配凭据"——界面据此换成引导配置而不是"连不上"。 */
  get isAuth(): boolean {
    return this.status === 401 || this.status === 403 || this.status === 503;
  }

  /** 是不是"后端根本没起来"。 */
  get isOffline(): boolean {
    return this.status === 0;
  }
}

function hintFor(status: number): string {
  switch (status) {
    case 0:
      return "请确认后端已启动：python -m lingxi（默认监听 127.0.0.1:8080）";
    case 401:
    case 403:
      return "管理凭据不匹配：在 .env.local 设置 NEXT_PUBLIC_ADMIN_TOKEN 为后端 LINGXI_SECURITY__ADMIN_TOKEN 的值，重启前端";
    case 503:
      return "后端未配置管理凭据且未以本地模式运行：设置 LINGXI_SECURITY__ADMIN_TOKEN，或确认 --dashboard-host 为 127.0.0.1";
    case 404:
      return "端点不存在：该功能需要对应模块已挂载（检查启动日志里的路由注册）";
    default:
      return status >= 500
        ? "后端内部错误：查看启动终端的异常堆栈"
        : "请检查请求参数";
  }
}

/**
 * 统一的响应处理：解析 FastAPI 的 `{"detail": ...}`，转成 ApiError。
 *
 * 此前各 fetch 直接 `throw new Error("API error: 503")` —— 把后端已经写好的
 * "请设置 LINGXI_SECURITY__ADMIN_TOKEN" 原文丢掉了，用户只看到一个编号。
 */
async function unwrap<T>(res: Response): Promise<T> {
  if (res.ok) return (await res.json()) as T;
  let detail = "";
  try {
    const body = await res.json();
    if (body && typeof body.detail === "string") detail = body.detail;
  } catch {
    /* 非 JSON 响应（如 502 网关 HTML）*/
  }
  throw new ApiError(res.status, detail, hintFor(res.status));
}

/** 统一 fetch 包装：带上凭据、禁用缓存、把失败转成 ApiError。 */
async function api<T>(
  path: string,
  init: RequestInit & { json?: unknown } = {},
): Promise<T> {
  const { json, ...rest } = init;
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...rest,
      headers: {
        ...authHeaders(),
        ...(json !== undefined ? { "Content-Type": "application/json" } : {}),
        ...(rest.headers as Record<string, string> | undefined),
      },
      body: json !== undefined ? JSON.stringify(json) : undefined,
      cache: "no-store",
    });
  } catch (e) {
    // fetch 本身抛错 = 网络层不可达（后端没起/端口不对/CORS）
    throw new ApiError(0, (e as Error).message || "网络请求失败");
  }
  return unwrap<T>(res);
}

/** 拿原始 Response（下载类接口），失败同样转 ApiError。 */
async function apiRaw(path: string): Promise<Response> {
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      headers: authHeaders(),
      cache: "no-store",
    });
    if (!res.ok) await unwrap(res);
    return res;
  } catch (e) {
    if (e instanceof ApiError) throw e;
    throw new ApiError(0, (e as Error).message || "网络请求失败");
  }
}

export interface DashboardSummary {
  active_tickets: number;
  escalated: number;
  processing: number;
  waiting_user: number;
  resolved: number;
  resolution_rate: number;
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
  sla: {
    total: number;
    breached: number;
    compliant: number;
    breach_rate: number;
  };
  seats: {
    total_seats: number;
    online_seats: number;
    busy_seats: number;
    idle_seats: number;
    total_capacity: number;
    used_capacity: number;
    utilization_rate: number;
    total_handled: number;
    avg_response_seconds: number;
  };
  avg_wait_seconds: number;
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
  return api<DashboardSummary>("/api/dashboard/summary");
}

export async function fetchTickets(status?: string): Promise<TicketSummary[]> {
  const qs = status ? `?status=${status}` : "";
  return api<TicketSummary[]>(`/api/tickets${qs}`);
}

export async function fetchQueue(): Promise<QueueSnapshot> {
  return api<QueueSnapshot>("/api/queue");
}

export async function claimTicket(agentId: string): Promise<{
  ticket_id: string;
  chat_id: string;
  category: string;
  reason: string;
} | null> {
  try {
    return await api<{
      ticket_id: string;
      chat_id: string;
      category: string;
      reason: string;
    }>(`/api/queue/claim?agent_id=${encodeURIComponent(agentId)}`, {
      method: "POST",
    });
  } catch (e) {
    // 后端空队列返回 404（REST 语义），前端映射为 null 表示"无可认领"
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
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
  return api<SessionInfo[]>("/api/sessions");
}

export async function fetchLogs(limit = 200): Promise<LogEntry[]> {
  return api<LogEntry[]>(`/api/logs?limit=${limit}`);
}

export interface CompleteResult {
  ticket_id: string;
  status: string;
}

export async function completeTicket(ticketId: string): Promise<CompleteResult> {
  return api<CompleteResult>(
    `/api/queue/complete?ticket_id=${encodeURIComponent(ticketId)}`,
    { method: "POST" },
  );
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
  return api<KnowledgeStats>("/api/knowledge/stats");
}

export async function fetchKnowledgeDocs(): Promise<KnowledgeDoc[]> {
  return api<KnowledgeDoc[]>("/api/knowledge/docs");
}

export async function uploadKnowledge(
  content: string,
  docId = "",
  source = "",
): Promise<{ doc_id: string; chunk_count: number; source: string }> {
  return api<{ doc_id: string; chunk_count: number; source: string }>(
    "/api/knowledge/upload",
    { method: "POST", json: { content, doc_id: docId, source } },
  );
}

export async function recallKnowledge(
  query: string,
  topK = 5,
): Promise<RecallHit[]> {
  return api<RecallHit[]>("/api/knowledge/recall", {
    method: "POST",
    json: { query, top_k: topK },
  });
}

export async function deleteKnowledgeDoc(docId: string): Promise<{ deleted_chunks: number }> {
  return api<{ deleted_chunks: number }>(
    `/api/knowledge/docs/${encodeURIComponent(docId)}`,
    { method: "DELETE" },
  );
}

export async function resetKnowledge(): Promise<{ status: string }> {
  return api<{ status: string }>("/api/knowledge/reset", {
    method: "POST",
  });
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
  return api<AuditEntry[]>(`/api/audit${qs ? `?${qs}` : ""}`);
}

export async function exportAudit(): Promise<void> {
  const res = await apiRaw("/api/audit/export");
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
  const data = await api<{ summary: string }>("/api/sessions/summarize", {
    method: "POST",
    json: { messages },
  });
  return data.summary;
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
  return api<Experiment[]>("/api/experiments");
}

export async function fetchExperiment(name: string): Promise<ExperimentDetail> {
  return api<ExperimentDetail>(`/api/experiments/${encodeURIComponent(name)}`);
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
  return api<{ name: string; status: string }>("/api/experiments", {
    method: "POST",
    json: config,
  });
}

export async function updateExperimentStatus(
  name: string,
  status: string,
): Promise<{ name: string; status: string }> {
  return api<{ name: string; status: string }>(
    `/api/experiments/${encodeURIComponent(name)}/status`,
    { method: "PUT", json: { status } },
  );
}

export async function deleteExperiment(name: string): Promise<{ deleted: string }> {
  return api<{ deleted: string }>(
    `/api/experiments/${encodeURIComponent(name)}`,
    { method: "DELETE" },
  );
}

export async function recordExperimentMetric(
  name: string,
  variantName: string,
  userId: string,
  resolved: boolean,
  escalated: boolean = false,
): Promise<{ recorded: boolean }> {
  return api<{ recorded: boolean }>(
    `/api/experiments/${encodeURIComponent(name)}/metrics`,
    {
      method: "POST",
      json: {
        variant_name: variantName,
        user_id: userId,
        resolved,
        escalated,
      },
    },
  );
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

  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method: opts.method ?? "GET",
      headers: {
        ...authHeaders(),
        ...(opts.body ? { "Content-Type": "application/json" } : {}),
    },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
      signal: controller.signal,
      cache: "no-store",
    });
  } catch (e) {
    // 网络层不可达：让调用方按 ApiError 处理，不要伪装成"SSE 连接失败"
    throw new ApiError(0, (e as Error).message || "SSE 网络请求失败");
  }

  if (!res.ok || !res.body) {
    // 复用 unwrap 的 detail 解析：SSE 也会被 503（未配凭据）挡住，
    // 报"连接失败"等于把后端写好的处置建议丢掉
    await unwrap(res);
    throw new ApiError(res.status, "SSE 响应没有可读流");
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
  return api<PersonaState>("/api/persona");
}

export async function previewPersona(
  message: string,
  memoryBlock = "",
  lorebookBlock = "",
): Promise<PersonaPreview> {
  return api<PersonaPreview>("/api/persona/preview", {
    method: "POST",
    json: {
      message,
      memory_block: memoryBlock,
      lorebook_block: lorebookBlock,
    },
  });
}
