/** @type {import('next').NextConfig} */
// 前端**直连**后端（http://127.0.0.1:8080），不再走 Next 代理。
//
// 为什么不代理：/api/events 是 SSE 长连接，rewrites 会缓冲响应体，
// 浏览器拿到 200 text/event-stream 却一帧都收不到 —— 实时事件流永远
// 卡在"连接中"。CORS 由后端 flow/dashboard_api.py::_install_cors 处理，
// 默认只放行 loopback 来源，与后端默认监听范围对齐。
//
// 跨机部署时：把目标 origin 加进 security.cors_origins，并设置
// NEXT_PUBLIC_API_BASE=http://<host>:8080。
const nextConfig = {
  reactStrictMode: true,
  output: process.env.TAURI === "1" ? "export" : undefined, // Tauri 模式静态导出
  images: { unoptimized: process.env.TAURI === "1" }, // 静态导出必须
};

module.exports = nextConfig;