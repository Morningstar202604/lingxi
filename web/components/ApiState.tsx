"use client";

// ── 统一的「取不到数据」呈现 ──
// 产品底线：**没配置也要能打开**。此前每个页面各自写了一套 error 分支，
// 结果是同一个问题（没配 admin_token）被渲染成 6 种不同的红字，
// 而且全都写成"无法连接后端" —— 误导用户去查网络，而不是去配凭据。
//
// 这里收口成三类，各自带**可执行的下一步**，而不是只报状态码：
//   离线 (status 0)   → 后端没起，给启动命令
//   未授权 (401/403/503) → 凭据没配/不匹配，给环境变量名
//   其他 (4xx/5xx)    → 透传后端 detail（后端通常已写清原因）

import type { ReactNode } from "react";

import { ApiError } from "@/lib/api";

export function classifyError(e: unknown): {
  kind: "offline" | "auth" | "server" | "unknown";
  title: string;
  detail: string;
  hint: string;
} {
  if (e instanceof ApiError) {
    if (e.isOffline) {
      return {
        kind: "offline",
        title: "后端未连接",
        detail: e.detail || "请求没能发出去",
        hint: e.hint,
      };
    }
    if (e.isAuth) {
      return {
        kind: "auth",
        title: "需要配置管理凭据",
        detail: e.detail || `HTTP ${e.status}`,
        hint:
          e.hint ||
          "设置 NEXT_PUBLIC_ADMIN_TOKEN 为后端的 LINGXI_SECURITY__ADMIN_TOKEN 后重启前端",
      };
    }
    return {
      kind: "server",
      title: `接口返回 ${e.status}`,
      detail: e.detail || `HTTP ${e.status}`,
      hint: e.hint,
    };
  }
  return {
    kind: "unknown",
    title: "出错了",
    detail: e instanceof Error ? e.message : String(e),
    hint: "",
  };
}

const TONE: Record<string, { border: string; badge: string; title: string }> = {
  offline: {
    border: "border-lingxi-orange/30",
    badge: "bg-lingxi-orange/10 text-lingxi-orange",
    title: "text-lingxi-orange",
  },
  auth: {
    border: "border-lingxi-blue/30",
    badge: "bg-lingxi-blue/10 text-lingxi-blue",
    title: "text-lingxi-blue",
  },
  server: {
    border: "border-lingxi-red/30",
    badge: "bg-lingxi-red/10 text-lingxi-red",
    title: "text-lingxi-red",
  },
  unknown: {
    border: "border-white/10",
    badge: "bg-white/5 text-white/70",
    title: "text-white/80",
  },
};

interface Props {
  error: unknown;
  onRetry?: () => void;
  /** 紧凑模式：嵌在卡片里（用于局部面板），而非整页 */
  compact?: boolean;
  children?: ReactNode;
}

/** 错误态：把后端原文与处置建议一起呈现，而不是丢掉。 */
export default function ApiErrorPanel({ error, onRetry, compact, children }: Props) {
  const { kind, title, detail, hint } = classifyError(error);
  const tone = TONE[kind] ?? TONE.unknown;

  return (
    <div
      className={`card ${tone.border} ${compact ? "p-3" : "p-5 text-center"}`}
      role="status"
    >
      <div className={compact ? "" : "flex flex-col items-center"}>
        <span
          className={`inline-block px-2 py-0.5 rounded text-[10px] font-medium ${tone.badge}`}
        >
          {title}
        </span>
        {children}
        <p className={`text-xs text-white/70 mt-2 break-words ${compact ? "" : "max-w-md"}`}>
          {detail}
        </p>
        {hint && (
          <p
            className={`text-xs text-lingxi-muted mt-1.5 break-words ${compact ? "" : "max-w-md"}`}
          >
            {hint}
          </p>
        )}
        {onRetry && (
          <button
            onClick={onRetry}
            className="mt-3 px-3 py-1.5 rounded-lg text-xs border border-white/10 bg-white/5 text-white/70 hover:bg-white/10 hover:text-white transition-colors"
          >
            重试
          </button>
        )}
      </div>
    </div>
  );
}

/** 空态：区分「真的没有数据」与「还没加载完」。 */
export function EmptyState({
  title,
  hint,
}: {
  title: string;
  hint?: string;
}) {
  return (
    <div className="text-center py-10">
      <p className="text-xs text-lingxi-muted">{title}</p>
      {hint && <p className="text-[11px] text-lingxi-muted/60 mt-1.5">{hint}</p>}
    </div>
  );
}

/** 占位骨架：加载中，避免整页空白闪烁。 */
export function Skeleton({ rows = 3, className = "" }: { rows?: number; className?: string }) {
  return (
    <div className={`space-y-2 ${className}`} aria-busy="true" aria-live="polite">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="h-9 rounded-lg bg-white/[0.04] animate-pulse" />
      ))}
    </div>
  );
}