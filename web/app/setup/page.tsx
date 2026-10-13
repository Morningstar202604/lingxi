"use client";

import { useCallback, useEffect, useState } from "react";
import {
  fetchSetupStatus,
  type CloudPreset,
  type SetupStatus,
} from "@/lib/api";
import ApiErrorPanel from "@/components/ApiState";

/**
 * 模型接入页。
 *
 * 设计立场：**云端 API 优先**（用户自己申请 Key 写环境变量，最省心、
 * 效果最好）；本地模型（Ollama）作为可选的离线/隐私方案，提供下载安装
 * 入口与拉模型命令，由用户自行决定是否安装。
 */
export default function SetupPage() {
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [copied, setCopied] = useState("");

  const load = useCallback(async () => {
    try {
      setStatus(await fetchSetupStatus());
      setError(null);
    } catch (e) {
      setError(e);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 10_000);
    return () => clearInterval(id);
  }, [load]);

  const copy = useCallback(async (key: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(key);
      setTimeout(() => setCopied(""), 1500);
    } catch {
      /* 剪贴板不可用（非 https/localhost）时静默，用户可手动选中复制 */
    }
  }, []);

  if (error) {
    return <ApiErrorPanel error={error} onRetry={load} />;
  }
  if (!status) {
    return <div className="card animate-pulse h-48" />;
  }

  const { llm, ollama, cloud_presets, env_file_hint } = status;
  const ollamaReady = ollama.reachable && ollama.models.length > 0;

  return (
    <div className="space-y-6 max-w-4xl">
      {/* ── 当前状态 ── */}
      <section className="card">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-white">当前模型</h3>
          <span
            className={`text-xs flex items-center gap-2 ${
              llm.ready ? "text-lingxi-green" : "text-lingxi-red"
            }`}
          >
            <span
              className={`w-2 h-2 rounded-full ${
                llm.ready ? "bg-lingxi-green" : "bg-lingxi-red"
              }`}
            />
            {llm.ready ? "可用" : "未就绪"}
          </span>
        </div>
        <div className="mt-3 grid grid-cols-2 md:grid-cols-4 gap-4 text-xs">
          <div>
            <div className="text-lingxi-muted">提供方</div>
            <div className="text-white mt-1">{llm.provider}</div>
          </div>
          <div>
            <div className="text-lingxi-muted">模型</div>
            <div className="text-white mt-1 break-all">{llm.model}</div>
          </div>
          <div>
            <div className="text-lingxi-muted">API 地址</div>
            <div className="text-white mt-1 break-all">
              {llm.base_url || "（默认）"}
            </div>
          </div>
          <div>
            <div className="text-lingxi-muted">API Key</div>
            <div className="text-white mt-1">
              {llm.api_key_configured ? "已配置" : "未配置"}
            </div>
          </div>
        </div>
        <p className="mt-3 text-xs text-lingxi-muted leading-relaxed">
          {env_file_hint}
        </p>
      </section>

      {/* ── 云端 API（推荐）── */}
      <section>
        <h3 className="text-sm font-semibold text-white mb-1">
          云端 API（推荐）
        </h3>
        <p className="text-xs text-lingxi-muted mb-3">
          到对应平台申请 API Key，把下面的环境变量写进配置文件后重启后端即可。
          云端模型在理解能力与稳定性上都明显更好，按量计费成本很低。
        </p>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {cloud_presets.map((preset: CloudPreset) => (
            <div key={preset.id} className="card">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-white">
                    {preset.label}
                  </span>
                  <span className="text-[10px] px-1.5 py-0.5 rounded border border-lingxi-orange/30 text-lingxi-orange">
                    {preset.tag}
                  </span>
                </div>
                <a
                  href={preset.key_page}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="text-xs text-lingxi-green hover:underline"
                >
                  申请 Key →
                </a>
              </div>
              <pre className="mt-3 p-3 rounded-lg bg-black/30 border border-white/5 text-[11px] leading-relaxed text-white/80 overflow-x-auto">
                {Object.entries(preset.env)
                  .map(([k, v]) => (v ? `${k}=${v}` : `${k}=`))
                  .join("\n")}
              </pre>
              <button
                onClick={() =>
                  copy(
                    preset.id,
                    Object.entries(preset.env)
                      .map(([k, v]) => (v ? `${k}=${v}` : `${k}=`))
                      .join("\n"),
                  )
                }
                className="mt-2 px-3 py-1.5 rounded-lg text-xs border border-white/10 text-white/70 hover:bg-white/5 transition-colors"
              >
                {copied === preset.id ? "已复制" : "复制变量"}
              </button>
            </div>
          ))}
        </div>
      </section>

      {/* ── 本地模型（可选）── */}
      <section className="card">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-white">
            本地模型（可选 · 离线/隐私场景）
          </h3>
          <span
            className={`text-xs flex items-center gap-2 ${
              ollamaReady ? "text-lingxi-green" : "text-lingxi-muted"
            }`}
          >
            <span
              className={`w-2 h-2 rounded-full ${
                ollamaReady ? "bg-lingxi-green" : "bg-lingxi-muted"
              }`}
            />
            {ollama.reachable ? "已检测到 Ollama" : "未安装/未运行"}
          </span>
        </div>

        {ollama.reachable && ollama.models.length > 0 && (
          <div className="mt-3 text-xs text-lingxi-muted">
            已安装模型：
            <span className="text-white/80">{ollama.models.join(" · ")}</span>
          </div>
        )}

        <p className="mt-3 text-xs text-lingxi-muted leading-relaxed">
          完全本地运行、数据不出机器；代价是需要下载模型（数 GB）且回答质量
          取决于本机硬件。需要时再装即可，不影响云端配置。
        </p>

        <div className="mt-3 flex flex-wrap items-center gap-3">
          {/* 安装按钮：直接跳转官方下载页（选择对应系统安装包） */}
          <a
            href={ollama.install_url}
            target="_blank"
            rel="noreferrer noopener"
            className="px-4 py-2 rounded-lg text-xs border border-lingxi-orange/40 bg-lingxi-orange/10 text-lingxi-orange hover:bg-lingxi-orange/20 transition-colors"
          >
            下载安装 Ollama
          </a>
          <button
            onClick={() => copy("pull", ollama.pull_hint)}
            className="px-4 py-2 rounded-lg text-xs border border-white/10 text-white/70 hover:bg-white/5 transition-colors"
          >
            {copied === "pull" ? "已复制" : "复制拉模型命令"}
          </button>
        </div>
        <pre className="mt-3 p-3 rounded-lg bg-black/30 border border-white/5 text-[11px] text-white/70 overflow-x-auto">
          {ollama.pull_hint}
        </pre>

        {ollama.reachable && (
          <p className="mt-3 text-[11px] text-lingxi-muted">
            使用本地模型：设置 LINGXI_LLM__PROVIDER=ollama 与
            LINGXI_LLM__MODEL=qwen2.5:7b（默认即此配置），重启后端生效。
          </p>
        )}
      </section>
    </div>
  );
}
