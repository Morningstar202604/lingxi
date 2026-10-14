"use client";

import { useCallback, useEffect, useState } from "react";
import {
  getEngineEndpoint,
  isMobileApp,
  isTauriRuntime,
  probeEngineEndpoint,
  setEngineEndpoint,
} from "@/lib/api";

/**
 * 引擎连接设置。
 *
 * 🔗 为什么需要这一页
 *     「智犀」是客户端 + 引擎服务架构。桌面版由 App 自动拉起本机引擎
 *     （地址端口都是运行时决定的，用户无需关心）；**移动端引擎不在手机上**
 *     —— OS 不给任何 App 跑 Python 进程的能力 —— 所以需要用户填写
 *     自己部署的引擎地址（局域网 / 公网 / VPN）。
 *
 *     这一页同时服务两类用户：
 *     - 移动端：首次启动必填（自动进入引导态）
 *     - 桌面端：想连远程引擎而非本机 sidecar 时改这里
 */
export default function EngineSettingsPage() {
  const [baseUrl, setBaseUrl] = useState("");
  const [token, setToken] = useState("");
  const [current, setCurrent] = useState<{ base: string; mode: string } | null>(null);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [inApp, setInApp] = useState(false);

  const load = useCallback(async () => {
    setInApp(isTauriRuntime());
    const ep = await getEngineEndpoint();
    if (ep) {
      setCurrent({ base: ep.base_url, mode: ep.mode });
      setBaseUrl(ep.base_url);
      setToken(ep.token);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleProbe = useCallback(async () => {
    setBusy("probe");
    setMessage(null);
    try {
      const text = await probeEngineEndpoint(baseUrl, token);
      setMessage({ ok: true, text });
    } catch (e) {
      setMessage({ ok: false, text: (e as Error).message || String(e) });
    } finally {
      setBusy("");
    }
  }, [baseUrl, token]);

  const handleSave = useCallback(async () => {
    setBusy("save");
    setMessage(null);
    try {
      await setEngineEndpoint(baseUrl, token);
      setMessage({ ok: true, text: "已保存，立即生效（页面数据会自动重新加载）" });
      await load();
    } catch (e) {
      setMessage({ ok: false, text: (e as Error).message || String(e) });
    } finally {
      setBusy("");
    }
  }, [baseUrl, token, load]);

  return (
    <div className="space-y-6 max-w-2xl">
      <section className="card">
        <h3 className="text-sm font-semibold text-white">当前连接</h3>
        {current ? (
          <div className="mt-3 text-xs space-y-1">
            <div>
              <span className="text-lingxi-muted">地址：</span>
              <span className="text-white/90 break-all">{current.base}</span>
            </div>
            <div>
              <span className="text-lingxi-muted">模式：</span>
              <span className="text-white/90">
                {current.mode === "local" ? "本机引擎（桌面版内置）" : "远程引擎"}
              </span>
            </div>
          </div>
        ) : (
          <p className="mt-3 text-xs text-lingxi-muted">
            {inApp
              ? "尚未配置引擎地址。填写下面信息后即可开始使用。"
              : "当前通过构建期环境变量连接（NEXT_PUBLIC_API_BASE）。"}
          </p>
        )}
      </section>

      {isMobileApp() && !current && (
        <section className="card border-lingxi-orange/40 bg-lingxi-orange/[0.06]">
          <div className="text-sm font-semibold text-lingxi-orange">
            首次使用：连接你的引擎
          </div>
          <p className="text-xs text-white/75 mt-2 leading-relaxed">
            智犀的引擎（AI 推理、工单、记忆）运行在你自己的服务器或电脑上，
            手机 App 只负责交互。请填写引擎地址 —— 通常是
            <code className="mx-1 px-1 rounded bg-black/30">http://&lt;服务器IP&gt;:8080</code>
            （引擎启动时会打印实际端口）。同一局域网直接填内网 IP；
            不在同一网络需先做端口转发或 VPN。
          </p>
        </section>
      )}

      <section className="card space-y-4">
        <h3 className="text-sm font-semibold text-white">连接配置</h3>

        <div>
          <label className="text-[11px] text-lingxi-muted block mb-1.5">
            引擎地址
          </label>
          <input
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="http://192.168.1.10:8080"
            inputMode="url"
            autoComplete="off"
            autoCapitalize="off"
            spellCheck={false}
            className="w-full px-3 py-2.5 rounded-lg bg-black/30 border border-white/10 text-sm text-white focus:border-lingxi-orange/50 focus:outline-none"
          />
          <p className="text-[11px] text-lingxi-muted mt-1.5">
            引擎启动日志里的 <code>ZHIXI_DASHBOARD_PORT=</code> 行就是端口号。
          </p>
        </div>

        <div>
          <label className="text-[11px] text-lingxi-muted block mb-1.5">
            管理令牌（engine 的 LINGXI_SECURITY__ADMIN_TOKEN）
          </label>
          <input
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="粘贴引擎的管理令牌"
            type="password"
            autoComplete="off"
            spellCheck={false}
            className="w-full px-3 py-2.5 rounded-lg bg-black/30 border border-white/10 text-sm text-white focus:border-lingxi-orange/50 focus:outline-none"
          />
          <p className="text-[11px] text-lingxi-muted mt-1.5">
            引擎以本机回环地址运行时可能无需令牌（本地免密模式）。
          </p>
        </div>

        <div className="flex flex-wrap gap-3">
          <button
            onClick={handleProbe}
            disabled={busy !== "" || !baseUrl.trim()}
            className="px-4 py-2.5 rounded-lg text-xs border border-white/15 text-white/80 hover:bg-white/5 transition-colors disabled:opacity-40"
          >
            {busy === "probe" ? "测试中..." : "测试连接"}
          </button>
          <button
            onClick={handleSave}
            disabled={busy !== "" || !baseUrl.trim()}
            className="px-4 py-2.5 rounded-lg text-xs border border-lingxi-green/30 bg-lingxi-green/10 text-lingxi-green hover:bg-lingxi-green/20 transition-colors disabled:opacity-40"
          >
            {busy === "save" ? "保存中..." : "保存并连接"}
          </button>
        </div>

        {message && (
          <div
            className={`text-xs p-3 rounded-lg border ${
              message.ok
                ? "border-lingxi-green/30 bg-lingxi-green/5 text-lingxi-green"
                : "border-lingxi-red/30 bg-lingxi-red/5 text-lingxi-red"
            }`}
          >
            {message.text}
          </div>
        )}
      </section>

      <section className="card">
        <h3 className="text-sm font-semibold text-white mb-2">怎么部署引擎？</h3>
        <ol className="text-xs text-lingxi-muted space-y-2 list-decimal list-inside leading-relaxed">
          <li>
            在有网络的机器上（服务器 / 家用电脑 / NAS）安装并启动：
            <code className="ml-1 px-1 rounded bg-black/30">python -m lingxi</code>
          </li>
          <li>
            配置模型：推荐云端 API（DeepSeek / 通义 / 智谱），
            把 Key 写进引擎的 <code className="px-1 rounded bg-black/30">.env</code>
          </li>
          <li>
            需要手机在外网访问时，让引擎监听 <code className="px-1 rounded bg-black/30">0.0.0.0</code>
            并设置 <code className="px-1 rounded bg-black/30">LINGXI_SECURITY__ADMIN_TOKEN</code>，
            同时做端口转发或 VPN —— 公网暴露务必配强令牌。
          </li>
        </ol>
      </section>
    </div>
  );
}
