"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { pingHealth } from "@/lib/api";

const NAV_LINKS = [
  { href: "/", label: "总览", icon: "◰" },
  { href: "/tickets", label: "工单", icon: "▤" },
  { href: "/queue", label: "队列", icon: "☷" },
  { href: "/approvals", label: "审批", icon: "⚑" },
  { href: "/seats", label: "坐席", icon: "☺" },
  { href: "/sessions", label: "会话", icon: "◉" },
  { href: "/persona", label: "人格", icon: "◈" },
  { href: "/rules", label: "规则", icon: "⚖" },
  { href: "/workflow", label: "工作流", icon: "⟐" },
  { href: "/knowledge", label: "知识库", icon: "📚" },
  { href: "/experiments", label: "A/B 实验", icon: "⚗" },
  { href: "/audit", label: "审计", icon: "🔏" },
  { href: "/logs", label: "日志", icon: "◫" },
  { href: "/setup", label: "模型", icon: "⚙" },
];

/** 侧栏底部的引擎状态。此前这里写死"引擎在线"+绿点 —— 后端没起也照样显示在线，
 *  用户先信了这个指示灯，才去怀疑真正的故障点。改为真实探测 /api/health
 *  （该端点不需要凭据，所以未配置时也能反映"后端在不在"）。 */
function useEngineStatus() {
  const [state, setState] = useState<"checking" | "up" | "down">("checking");

  useEffect(() => {
    let alive = true;
    const ping = async () => {
      const ok = await pingHealth();
      if (alive) setState(ok ? "up" : "down");
    };
    ping();
    const id = setInterval(ping, 10_000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  return state;
}

export default function Sidebar() {
  const pathname = usePathname() || "/";
  const engine = useEngineStatus();
  const dot =
    engine === "up" ? "bg-lingxi-green" : engine === "down" ? "bg-lingxi-red" : "bg-lingxi-muted";
  const label =
    engine === "up" ? "引擎在线" : engine === "down" ? "引擎未连接" : "检测中…";

  const isActive = (href: string) =>
    pathname === href || (href !== "/" && pathname.startsWith(href));

  return (
    <>
      {/* 移动端导航（<md）：顶部横向滚动条。此前小屏完全没有导航 —
          页面能不能到达全靠手输 URL（Sidebar 整体 hidden md:flex）。 */}
      <nav className="md:hidden sticky top-0 z-30 flex items-center gap-1 overflow-x-auto px-3 py-2 bg-lingxi-navy/95 border-b border-white/5 backdrop-blur-md">
        <span className="shrink-0 w-6 h-6 mr-1 rounded-md bg-lingxi-orange/20 border border-lingxi-orange/40 flex items-center justify-center text-[10px] font-bold text-lingxi-orange">
          LX
        </span>
        {NAV_LINKS.map((link) => (
          <Link
            key={link.href}
            href={link.href}
            className={`shrink-0 px-2.5 py-1.5 rounded-lg text-xs whitespace-nowrap transition-colors ${
              isActive(link.href)
                ? "bg-lingxi-orange/10 text-lingxi-orange"
                : "text-white/60 hover:bg-white/5 hover:text-white/90"
            }`}
          >
            {link.label}
          </Link>
        ))}
        <span className={`ml-auto shrink-0 w-2 h-2 rounded-full ${dot} pulse-dot`} />
      </nav>

      <aside className="hidden md:flex w-56 flex-col bg-lingxi-navy/80 border-r border-white/5 p-4">
        <div className="flex items-center gap-3 px-3 py-4">
          <div className="w-8 h-8 rounded-lg bg-lingxi-orange/20 border border-lingxi-orange/40 flex items-center justify-center">
            <span className="text-lingxi-orange text-sm font-bold">LX</span>
          </div>
          <div>
            <h1 className="text-sm font-semibold text-white">智犀</h1>
            <p className="text-xs text-lingxi-muted">ZhiXi Engine</p>
          </div>
        </div>
        <nav className="mt-6 flex flex-col gap-1">
          {NAV_LINKS.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className={`px-3 py-2 rounded-lg text-sm transition-colors flex items-center gap-2.5 ${
                isActive(link.href)
                  ? "bg-lingxi-orange/10 text-lingxi-orange"
                  : "text-white/60 hover:bg-white/5 hover:text-white/90"
              }`}
            >
              <span className="text-xs opacity-60">{link.icon}</span>
              {link.label}
            </Link>
          ))}
        </nav>
        <div className="mt-auto pt-4 border-t border-white/5 space-y-2">
          <div className="flex items-center gap-2 px-3 text-xs text-lingxi-muted">
            <span className={`w-2 h-2 rounded-full ${dot} pulse-dot`} />
            {label}
          </div>
          <div className="px-3 text-[10px] text-lingxi-muted/50">
            ZhiXi v0.2.0
          </div>
        </div>
      </aside>
    </>
  );
}
