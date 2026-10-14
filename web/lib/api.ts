"use client";

// ── Dashboard API client ──

// 安全：NEXT_PUBLIC_* 会被编译进客户端 bundle 下发给每个访客，
// 绝不能内置管理凭据默认值。凭据必须由运维在登录后注入。
const ENV_API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://127.0.0.1:8080";
const ENV_ADMIN_TOKEN = process.env.NEXT_PUBLIC_ADMIN_TOKEN || "";

// 前端**直连**后端，不走 Next rewrites 代理。
// 原因：/api/events 是 SSE 长连接，Next dev 的 rewrites 会缓冲响应体 ——
// 浏览器拿到 200 text/event-stream 却一帧都收不到（实测复现）。
// 代理只对普通请求友好，对流式是死路；后端已开 loopback CORS（见
// flow/dashboard_api.py::_install_cors），dev 与 Tauri 因此行为一致。
//
// 地址与凭据在**运行时**解析：
// - 浏览器（dev/部署）：NEXT_PUBLIC_API_BASE / NEXT_PUBLIC_ADMIN_TOKEN 兜底
// - Tauri 桌面版：后端端口是动态的（8080 被占会自动顺延），令牌是每次
//   安装随机生成的 —— 通过 invoke("get_backend_port"/"get_admin_token")
//   向壳进程要真实值；后端重启换端口后（网络层失败）会重新解析。
interface RuntimeInfo {
  base: string;
  token: string;
}

let runtimePromise: Promise<RuntimeInfo> | null = null;

function isTauriRuntime(): boolean {
  return (
    typeof window !== "undefined" && "__TAURI_INTERNALS__" in (window as object)
  );
}

async function resolveRuntime(): Promise<RuntimeInfo> {
  if (runtimePromise) return runtimePromise;
  runtimePromise = (async (): Promise<RuntimeInfo> => {
    if (!isTauriRuntime()) {
      return { base: ENV_API_BASE, token: ENV_ADMIN_TOKEN };
    }
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const [port, token] = await Promise.all([
        invoke<number>("get_backend_port"),
        invoke<string>("get_admin_token"),
      ]);
      if (typeof port === "number" && port > 0) {
        return { base: `http://127.0.0.1:${port}`, token: token || "" };
      }
      return { base: ENV_API_BASE, token: ENV_ADMIN_TOKEN };
    } catch {
      // 壳命令不可用（例如纯浏览器访问 build 产物）→ 退回构建期配置
      return { base: ENV_API_BASE, token: ENV_ADMIN_TOKEN };
    }
  })();
  return runtimePromise;
}

/** 网络层失败（后端重启/换端口）后调用：下次请求重新解析地址与令牌。 */
export function invalidateRuntime(): void {
  runtimePromise = null;
}

/** 是否已有管理凭据（仅用于界面提示，不阻断请求）。 */
export function hasAdminToken(): boolean {
  // 桌面版令牌由 Tauri 壳在运行时注入，必然存在
  return Boolean(ENV_ADMIN_TOKEN) || isTauriRuntime();
}

/** 真实探测后端健康（Sidebar 状态灯用；非 Tauri 与 Tauri 走同一解析）。 */
export async function pingHealth(): Promise<boolean> {
  try {
    const rt = await resolveRuntime();
    const res = await fetch(`${rt.base}/api/health`, { cache: "no-store" });
    return res.ok;
  } catch {
    return false;
  }
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
  const rt = await resolveRuntime();
  let res: Response;
  try {
    res = await fetch(`${rt.base}${path}`, {
      ...rest,
      headers: {
        ...(rt.token ? { "x-admin-token": rt.token } : {}),
        ...(json !== undefined ? { "Content-Type": "application/json" } : {}),
        ...(rest.headers as Record<string, string> | undefined),
      },
      body: json !== undefined ? JSON.stringify(json) : undefined,
      cache: "no-store",
    });
  } catch (e) {
    // fetch 本身抛错 = 网络层不可达（后端没起/端口不对/CORS）。
    // 后端可能正在重启或换了端口 → 让下次请求重新解析运行时地址。
    invalidateRuntime();
    throw new ApiError(0, (e as Error).message || "网络请求失败");
  }
  return unwrap<T>(res);
}

/** 拿原始 Response（下载 / text/plain 接口），失败同样转 ApiError。 */
async function apiRaw(path: string, init: RequestInit = {}): Promise<Response> {
  const rt = await resolveRuntime();
  try {
    const res = await fetch(`${rt.base}${path}`, {
      ...init,
      headers: {
        ...(rt.token ? { "x-admin-token": rt.token } : {}),
        ...(init.headers as Record<string, string> | undefined),
      },
      cache: "no-store",
    });
    if (!res.ok) await unwrap(res);
    return res;
  } catch (e) {
    if (e instanceof ApiError) throw e;
    invalidateRuntime();
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
  const rt = await resolveRuntime();
  try {
    res = await fetch(`${rt.base}${path}`, {
      method: opts.method ?? "GET",
      headers: {
        ...(rt.token ? { "x-admin-token": rt.token } : {}),
        ...(opts.body ? { "Content-Type": "application/json" } : {}),
      },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      signal: controller.signal,
      cache: "no-store",
    });
  } catch (e) {
    // 网络层不可达：让调用方按 ApiError 处理，不要伪装成"SSE 连接失败"
    invalidateRuntime();
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

// ── 模型接入状态（/setup 页）──

export interface CloudPreset {
  id: string;
  label: string;
  tag: string;
  provider: string;
  model: string;
  base_url: string;
  env: Record<string, string>;
  key_page: string;
}

export interface SetupStatus {
  llm: {
    provider: string;
    model: string;
    base_url: string | null;
    api_key_configured: boolean;
    ready: boolean;
    mode: "cloud" | "local_ready" | "unconfigured";
  };
  recommendation: {
    primary: string;
    text: string;
  };
  ollama: {
    reachable: boolean;
    base_url: string;
    models: string[];
    install_url: string;
    pull_hint: string;
  };
  cloud_presets: CloudPreset[];
  env_file_hint: string;
}

export async function fetchSetupStatus(): Promise<SetupStatus> {
  return api<SetupStatus>("/api/setup/status");
}

// ── 人工审批（L2/L3 高风险操作，agent/approval.py）──

export interface ApprovalItem {
  approval_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  required_level: number;
  requester_level: number;
  chat_id: string;
  user_id: string;
  channel: string;
  created_at: number;
  status: string;
  approver: string;
  decided_at: number;
  note: string;
}

export interface ApprovalDecision {
  approval_id: string;
  status: string;
  approver: string;
  resumed: boolean;
  final_reply: string;
  escalate_reason: string;
}

export async function fetchApprovals(status = "pending"): Promise<ApprovalItem[]> {
  return api<ApprovalItem[]>(`/api/approvals?status=${encodeURIComponent(status)}`);
}

export async function decideApproval(
  approvalId: string,
  action: "approve" | "reject",
  approver = "",
  note = "",
): Promise<ApprovalDecision> {
  return api<ApprovalDecision>(`/api/approvals/${approvalId}/${action}`, {
    method: "POST",
    json: { approver, note },
  });
}

// ── 坐席管理（seat/router.py）──

export interface Seat {
  username: string;
  display_name: string;
  status: string;
  max_concurrent: number;
  current_ticket_count: number;
  total_handled: number;
  avg_response_seconds: number;
  has_capacity: boolean;
}

export interface SeatMetrics {
  total_seats: number;
  online_seats: number;
  busy_seats: number;
  idle_seats: number;
  total_capacity: number;
  used_capacity: number;
  utilization_rate: number;
  total_handled: number;
}

export async function fetchSeats(): Promise<Seat[]> {
  return api<Seat[]>("/api/seats");
}

export async function fetchSeatMetrics(): Promise<SeatMetrics> {
  return api<SeatMetrics>("/api/seats/metrics");
}

export async function registerSeat(
  username: string,
  displayName = "",
  maxConcurrent = 3,
): Promise<Seat> {
  return api<Seat>("/api/seats", {
    method: "POST",
    json: {
      username,
      display_name: displayName,
      max_concurrent: maxConcurrent,
    },
  });
}

export async function updateSeatStatus(
  username: string,
  status: string,
): Promise<Seat> {
  return api<Seat>(`/api/seats/${encodeURIComponent(username)}/status`, {
    method: "PUT",
    json: { status },
  });
}

// ── 规则引擎（flow/rules.py）──

export interface RuleCondition {
  field: string;
  operator: string;
  value: unknown;
}

export interface RuleAction {
  action: string;
  params: Record<string, unknown>;
}

export interface AutomationRule {
  name: string;
  enabled?: boolean;
  conditions?: RuleCondition[];
  actions?: RuleAction[];
  priority?: number;
}

export async function fetchRules(): Promise<AutomationRule[]> {
  return api<AutomationRule[]>("/api/rules");
}

/** 注意：后端用 Rule(**body) 严格校验，body 必须是完整 Rule 形状。 */
export async function addRule(rule: AutomationRule): Promise<unknown> {
  return api("/api/rules", { method: "POST", json: rule });
}

export async function deleteRule(name: string): Promise<unknown> {
  return api(`/api/rules/${encodeURIComponent(name)}`, { method: "DELETE" });
}

export async function evaluateRules(
  event: Record<string, unknown>,
): Promise<{ matched_actions: RuleAction[] }> {
  return api<{ matched_actions: RuleAction[] }>("/api/rules/evaluate", {
    method: "POST",
    json: { event },
  });
}

// ── SLA 监控 ──

export interface SlaStatus {
  summary: {
    total: number;
    breached: number;
    compliant: number;
    breach_rate: number;
  };
  breaches: unknown[];
  sla_config: Record<string, unknown>;
}

export async function fetchSlaStatus(): Promise<SlaStatus> {
  return api<SlaStatus>("/api/sla/status");
}

// ── 工作流 DAG（workflow/router.py）──
// /graph 与 /graph/preview 返回 text/plain（Mermaid 源码），走 apiRaw 拿原文。

export async function fetchWorkflowGraph(): Promise<string> {
  const res = await apiRaw("/api/workflow/graph");
  return res.text();
}

export async function fetchWorkflowYaml(): Promise<string> {
  const res = await apiRaw("/api/workflow/graph/yaml");
  return res.text();
}

export async function previewWorkflowGraph(yamlText: string): Promise<string> {
  const res = await apiRaw("/api/workflow/graph/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ yaml: yamlText }),
  });
  return res.text();
}

export async function validateWorkflow(body: Record<string, unknown>): Promise<{
  valid: boolean;
  errors: string[];
  node_count: number;
  edge_count: number;
  entry_nodes: string[];
  exit_nodes: string[];
}> {
  return api("/api/workflow/graph/validate", { method: "POST", json: body });
}

export async function executeWorkflow(body: Record<string, unknown>): Promise<{
  [key: string]: unknown;
}> {
  return api("/api/workflow/execute", { method: "POST", json: body });
}
