import type { Metadata, Viewport } from "next";
import Clock from "@/components/Clock";
import Sidebar from "@/components/Sidebar";
import MobileNav from "@/components/MobileNav";
import PwaRegister from "@/components/PwaRegister";
import "./globals.css";

export const metadata: Metadata = {
  title: "智犀 ZhiXi — 数字员工控制台",
  description: "24h 全栈数字员工引擎 · 智辨已知，犀照善为",
  // PWA：Android 下可直接"安装到主屏幕"，独立窗口运行（无浏览器地址栏）
  manifest: "/manifest.webmanifest",
  applicationName: "智犀",
  appleWebApp: {
    capable: true,
    statusBarStyle: "black-translucent",
    title: "智犀",
  },
  icons: {
    icon: "/icon.svg",
    apple: "/icon-192.png",
  },
};

// viewport 用官方 export 而非手写 <meta>：Next.js 14 会去重并注入正确的
// 移动端参数（width=device-width 不写的话 Android 按 980px 虚拟视口渲染，
// 整站会被缩成一小条）。
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  // 允许用户缩放（可访问性要求，禁缩放会让视力障碍用户无法放大正文）
  maximumScale: 5,
  viewportFit: "cover", // 配合 env(safe-area-inset-*) 覆盖刘海屏
  themeColor: "#0f1729",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh-CN">
      <body className="font-sans antialiased">
        <PwaRegister />
        {/* 小屏纵向堆叠（移动端导航条 + 内容），md 起恢复侧栏 + 内容两栏 */}
        <div className="flex flex-col md:flex-row min-h-screen">
          <Sidebar />
          <main className="flex-1 min-w-0 overflow-auto">
            {/* 桌面顶栏；小屏由 MobileNav 承担导航与状态，不重复占高度 */}
            <header className="hidden md:flex sticky top-0 z-10 backdrop-blur-md bg-[#0f1729]/80 border-b border-white/5 px-6 py-3 items-center justify-between">
              <h2 className="text-sm font-medium text-white/80">智犀控制台 · ZhiXi v0.2.0</h2>
              <div className="flex items-center gap-3 text-xs text-lingxi-muted">
                <Clock />
              </div>
            </header>
            <div className="p-4 md:p-6">{children}</div>
          </main>
        </div>
        {/* 移动端底部导航（小屏才显示）：拇指可达区，替代桌面侧栏 */}
        <MobileNav />
      </body>
    </html>
  );
}
