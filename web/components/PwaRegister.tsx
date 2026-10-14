"use client";

import { useEffect, useState } from "react";

/**
 * PWA 支持：Service Worker 注册 + Android 安装引导。
 *
 * Android 上走官方安装流程（beforeinstallprompt + 用户手势触发 prompt），
 * 而不是自造"添加到主屏幕"说明 —— 浏览器原生弹窗的接受率与兼容性都更好。
 * 已安装（standalone）或浏览器不支持时静默不显示。
 */

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

export default function PwaRegister() {
  const [installEvent, setInstallEvent] = useState<BeforeInstallPromptEvent | null>(null);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    // Service Worker：仅在生产构建注册（dev 下 SW 会缓存住热更新）
    if ("serviceWorker" in navigator && process.env.NODE_ENV === "production") {
      navigator.serviceWorker.register("/sw.js").catch(() => {
        /* 注册失败不影响使用（离线能力降级，在线功能不受影响） */
      });
    }

    const onPrompt = (e: Event) => {
      // 拦截浏览器默认横幅，改为在合适时机由用户主动触发（转化率更高）
      e.preventDefault();
      setInstallEvent(e as BeforeInstallPromptEvent);
    };
    window.addEventListener("beforeinstallprompt", onPrompt);
    return () => window.removeEventListener("beforeinstallprompt", onPrompt);
  }, []);

  if (!installEvent || dismissed) return null;

  return (
    <button
      onClick={async () => {
        await installEvent.prompt();
        const choice = await installEvent.userChoice;
        if (choice.outcome === "accepted") setInstallEvent(null);
        setDismissed(true);
      }}
      className="md:hidden fixed top-3 right-3 z-50 px-3 py-2 rounded-lg text-xs bg-lingxi-orange text-[#0f1729] font-medium shadow-lg safe-top"
    >
      安装到桌面
    </button>
  );
}
