"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV_LINKS = [
  { href: "/", label: "总览", icon: "◰" },
  { href: "/tickets", label: "工单", icon: "▤" },
  { href: "/queue", label: "队列", icon: "☷" },
  { href: "/sessions", label: "会话", icon: "◉" },
  { href: "/persona", label: "人格", icon: "◈" },
  { href: "/knowledge", label: "知识库", icon: "📚" },
  { href: "/experiments", label: "A/B 实验", icon: "⚗" },
  { href: "/audit", label: "审计", icon: "🔏" },
  { href: "/logs", label: "日志", icon: "◫" },
];

export default function Sidebar() {
  const pathname = usePathname() || "/";
  return (
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
        {NAV_LINKS.map((link) => {
          const active =
            pathname === link.href ||
            (link.href !== "/" && pathname.startsWith(link.href));
          return (
            <Link
              key={link.href}
              href={link.href}
              className={`px-3 py-2 rounded-lg text-sm transition-colors flex items-center gap-2.5 ${
                active
                  ? "bg-lingxi-orange/10 text-lingxi-orange"
                  : "text-white/60 hover:bg-white/5 hover:text-white/90"
              }`}
            >
              <span className="text-xs opacity-60">{link.icon}</span>
              {link.label}
            </Link>
          );
        })}
      </nav>
      <div className="mt-auto pt-4 border-t border-white/5 space-y-2">
        <div className="flex items-center gap-2 px-3 text-xs text-lingxi-muted">
          <span className="w-2 h-2 rounded-full bg-lingxi-green pulse-dot" />
          引擎在线
        </div>
        <div className="px-3 text-[10px] text-lingxi-muted/50">
          ZhiXi v0.2.0 · 395 tests
        </div>
      </div>
    </aside>
  );
}
