/** @type {import('next').NextConfig} */
// 前端经 Next 代理访问后端（同源，无需 CORS）。
// 后端地址可用 DASHBOARD_API_URL 覆盖，见 web/lib/api.ts。
const API_TARGET = process.env.DASHBOARD_API_URL || "http://127.0.0.1:8080";

const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    return [
      {
        // source 剥掉 /api/proxy 后，剩余部分已是完整后端路径
        // （前端请求 /api/proxy/api/xxx）：不要再补 /api，
        // 否则会拼成 /api/api/xxx → FastAPI 404（E2E 实测踩过）。
        source: "/api/proxy/:path*",
        destination: `${API_TARGET}/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
