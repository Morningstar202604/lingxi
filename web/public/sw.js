/* 智犀 PWA Service Worker。
 *
 * 策略（保持简单且正确，不引第三方框架）：
 *  - 静态资源（/_next/static、图标）：cache-first —— 构建产物带内容哈希，
 *    不会失效，可长期缓存。
 *  - 导航请求（HTML）：network-first，失败时回退缓存 —— 保证拿到最新页面，
 *    离线时仍能打开壳。
 *  - /api/* 与 SSE/流式端点：**一律不缓存**（管理数据实时性优先；
 *    缓存工单/审批会造成"看到旧状态"的误判）。
 */
const VERSION = "zhixi-v1";
const STATIC_CACHE = `${VERSION}-static`;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) =>
      cache.addAll(["/", "/manifest.webmanifest", "/icon.svg"]).catch(() => undefined),
    ),
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => !k.startsWith(VERSION)).map((k) => caches.delete(k))),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);

  // API / 事件流：直连网络，绝不缓存
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/mcp/")) return;

  // 静态资源：cache-first
  if (url.pathname.startsWith("/_next/static/") || url.pathname.startsWith("/icon")) {
    event.respondWith(
      caches.match(request).then(
        (hit) =>
          hit ||
          fetch(request).then((res) => {
            const copy = res.clone();
            caches.open(STATIC_CACHE).then((c) => c.put(request, copy));
            return res;
          }),
      ),
    );
    return;
  }

  // 导航 / 其他：network-first，失败回退缓存
  event.respondWith(
    fetch(request)
      .then((res) => {
        const copy = res.clone();
        caches.open(STATIC_CACHE).then((c) => c.put(request, copy));
        return res;
      })
      .catch(() => caches.match(request).then((hit) => hit || caches.match("/"))),
  );
});
