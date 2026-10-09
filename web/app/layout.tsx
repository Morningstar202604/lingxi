import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "智犀 ZhiXi — 数字员工控制台",
  description: "24h 全栈数字员工引擎 · 智辨已知，犀照善为",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh-CN">
      <body className="font-sans antialiased">
        <div className="flex min-h-screen">
          {/* Sidebar */}
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
              <NavItem href="/" label="总览" active />
              <NavItem href="/tickets" label="工单" />
              <NavItem href="/queue" label="队列" />
              <NavItem href="/sessions" label="会话" />
              <NavItem href="/logs" label="日志" />
            </nav>
            <div className="mt-auto pt-4 border-t border-white/5">
              <div className="flex items-center gap-2 px-3 text-xs text-lingxi-muted">
                <span className="w-2 h-2 rounded-full bg-lingxi-green pulse-dot" />
                引擎在线
              </div>
            </div>
          </aside>
          {/* Main */}
          <main className="flex-1 overflow-auto">
            <header className="sticky top-0 z-10 backdrop-blur-md bg-[#0f1729]/80 border-b border-white/5 px-6 py-3 flex items-center justify-between">
              <h2 className="text-sm font-medium text-white/80">实时监控面板</h2>
              <div className="flex items-center gap-3 text-xs text-lingxi-muted">
                <span id="clock">--:--:--</span>
              </div>
            </header>
            <div className="p-6">{children}</div>
          </main>
        </div>
      </body>
    </html>
  );
}

function NavItem({ href, label, active }: { href: string; label: string; active?: boolean }) {
  return (
    <a
      href={href}
      className={`px-3 py-2 rounded-lg text-sm transition-colors ${
        active
          ? "bg-lingxi-orange/10 text-lingxi-orange"
          : "text-white/60 hover:bg-white/5 hover:text-white/90"
      }`}
    >
      {label}
    </a>
  );
}
