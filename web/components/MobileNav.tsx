"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { pingHealth } from "@/lib/api";

/**
 * 移动端底部导航（仅 <md 显示）。
 *
 * 为什么用底部而非顶部：手机竖屏时拇指自然覆盖区在屏幕下 1/3，
 * Android Material 与 iOS HIG 都把主导航放在底部。桌面端保留侧栏
 * （信息密度更高），两者按断点互斥，不重复占空间。
 *
 * 分两级：高频 4 项常驻底栏，其余收进"更多"面板 —— 14 个页面全塞底栏
 * 会让每项窄到点不中（触控目标 <44px）。
 */

const PRIMARY = [
  { href: "/", label: "总览", icon: "◰" },
  { href: "/queue", label: "队列", icon: "☷" },
  { href: "/approvals", label: "审批", icon: "⚑" },
  { href: "/sessions", label: "会话", icon: "◉" },
];

const MORE = [
  { href: "/tickets", label: "工单", icon: "▤" },
  { href: "/seats", label: "坐席", icon: "☺" },
  { href: "/persona", label: "人格", icon: "◈" },
  { href: "/rules", label: "规则", icon: "⚖" },
  { href: "/workflow", label: "工作流", icon: "⟐" },
  { href: "/knowledge", label: "知识库", icon: "📚" },
  { href: "/experiments", label: "A/B 实验", icon: "⚗" },
  { href: "/audit", label: "审计", icon: "🔏" },
  { href: "/logs", label: "日志", icon: "◫" },
  { href: "/setup", label: "模型", icon: "⚙" },
  { href: "/engine", label: "连接", icon: "⇄" },
];

function useEngineUp() {
  const [up, setUp] = useState<boolean | null>(null);
  useEffect(() => {
    let alive = true;
    const ping = async () => {
      const ok = await pingHealth();
      if (alive) setUp(ok);
    };
    ping();
    const id = setInterval(ping, 15_000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);
  return up;
}

export default function MobileNav() {
  const pathname = usePathname() || "/";
  const up = useEngineUp();
  const [moreOpen, setMoreOpen] = useState(false);

  const isActive = (href: string) =>
    pathname === href || (href !== "/" && pathname.startsWith(href));

  return (
    <>
      {/* 更多面板：从底部弹出的抽屉 */}
      {moreOpen && (
        <div className="md:hidden fixed inset-0 z-40" role="dialog" aria-label="更多页面">
          {/* 点背景关闭（移动端标准手势） */}
          <button
            aria-label="关闭"
            className="absolute inset-0 bg-black/50"
            onClick={() => setMoreOpen(false)}
          />
          <nav className="absolute bottom-0 left-0 right-0 bg-lingxi-navy border-t border-white/10 rounded-t-2xl p-4 safe-bottom">
            <div className="w-10 h-1 rounded-full bg-white/20 mx-auto mb-3" />
            <div className="grid grid-cols-4 gap-2">
              {MORE.map((link) => (
                <Link
                  key={link.href}
                  href={link.href}
                  onClick={() => setMoreOpen(false)}
                  className={`flex flex-col items-center gap-1 py-3 rounded-xl text-[11px] transition-colors ${
                    isActive(link.href)
                      ? "bg-lingxi-orange/10 text-lingxi-orange"
                      : "text-white/70 active:bg-white/5"
                  }`}
                >
                  <span className="text-base">{link.icon}</span>
                  {link.label}
                </Link>
              ))}
            </div>
          </nav>
        </div>
      )}

      {/* 底栏本体 */}
      <nav className="md:hidden fixed bottom-0 left-0 right-0 z-30 bg-lingxi-navy/95 backdrop-blur-md border-t border-white/10 safe-bottom">
        <div className="grid grid-cols-5">
          {PRIMARY.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className={`flex flex-col items-center justify-center gap-0.5 py-2.5 text-[10px] transition-colors ${
                isActive(link.href)
                  ? "text-lingxi-orange"
                  : "text-white/55 active:text-white/90"
              }`}
            >
              <span className="text-lg leading-none">{link.icon}</span>
              {link.label}
            </Link>
          ))}
          <button
            onClick={() => setMoreOpen(true)}
            className={`flex flex-col items-center justify-center gap-0.5 py-2.5 text-[10px] transition-colors ${
              MORE.some((l) => isActive(l.href))
                ? "text-lingxi-orange"
                : "text-white/55 active:text-white/90"
            }`}
          >
            <span className="text-lg leading-none">⋯</span>
            更多
          </button>
        </div>
        {/* 引擎状态条：小屏没有侧栏指示灯，这里补一条极细的状态线 */}
        <div
          className={`h-0.5 ${
            up === true
              ? "bg-lingxi-green"
              : up === false
                ? "bg-lingxi-red"
                : "bg-lingxi-muted"
          }`}
        />
      </nav>

      {/* 底栏高度占位，避免内容被固定底栏遮住最后一段 */}
      <div className="md:hidden h-[68px]" aria-hidden />
    </>
  );
}
