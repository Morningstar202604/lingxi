/** @type {import('next').NextConfig} */
// 前端经 Next 代理访问后端（同源，无需 CORS）。
// 后端地址可用 DASHBOARD_API_URL 覆盖，见 web/lib/api.ts。
const API_TARGET = process.env.DASHBOARD_API_URL || "http://127.0.0.1:8080";
const IS_TAURI = process.env.TAURI === "1";

const nextConfig = {
  reactStrictMode: true,
  output: IS_TAURI ? "export" : undefined,     // Tauri 模式静态导出
  images: { unoptimized: IS_TAURI },           // 静态导出必须
  async rewrites() {
    if (IS_TAURI) return [];                   // Tauri 直接访问 sidecar，无需代理
    return [
      {
        source: "/api/proxy/:path*",
        destination: `${API_TARGET}/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;