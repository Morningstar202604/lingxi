import type { Metadata } from "next";
import Clock from "@/components/Clock";
import Sidebar from "@/components/Sidebar";
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
          <Sidebar />
          <main className="flex-1 overflow-auto">
            <header className="sticky top-0 z-10 backdrop-blur-md bg-[#0f1729]/80 border-b border-white/5 px-6 py-3 flex items-center justify-between">
              <h2 className="text-sm font-medium text-white/80">智犀控制台 · ZhiXi v0.2.0</h2>
              <div className="flex items-center gap-3 text-xs text-lingxi-muted">
                <Clock />
              </div>
            </header>
            <div className="p-6">{children}</div>
          </main>
        </div>
      </body>
    </html>
  );
}
