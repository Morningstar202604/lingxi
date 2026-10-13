"use client";

// ── 人格控制台 ──
// 数据源改为真实的 /api/persona 与 /api/persona/preview。
// 此前这一页的 identity / activeModules 全是前端写死的常量，keywordRulesCount
// 恒为 0，编辑按钮则弹一句"后端 PUT 待实现"——运维在控制台看到的不是引擎
// 真正在跑的人格配置。

import { useCallback, useEffect, useState } from "react";

import { fetchPersona, previewPersona, type PersonaState } from "@/lib/api";
import ApiErrorPanel from "@/components/ApiState";

const MODULE_LABELS: Record<string, string> = {
  time_awareness: "时间感知",
  turn_stage_awareness: "对话阶段感知",
  keyword_triggers: "关键词触发",
  emotion_vector: "情绪向量",
  memory_injection: "记忆注入",
  lorebook_injection: "世界观注入",
  variance: "表达变体",
  fixed_signals: "固定信号",
  static_rules: "静态规则",
  translate: "语言适配",
};

const ACTION_LABELS: Record<string, string> = {
  inject_prompt: "注入提示",
  switch_emotion: "切换情绪",
  escalate: "强制升级",
};

const VERDICT_STYLE: Record<string, string> = {
  clean: "border-lingxi-green/30 text-lingxi-green bg-lingxi-green/10",
  suspect: "border-lingxi-orange/30 text-lingxi-orange bg-lingxi-orange/10",
  block: "border-lingxi-red/30 text-lingxi-red bg-lingxi-red/10",
};

export default function PersonaPage() {
  const [state, setState] = useState<PersonaState | null>(null);
  const [error, setError] = useState<unknown>(null);

  const [probe, setProbe] = useState("我要退货");
  const [prompt, setPrompt] = useState<string | null>(null);
  const [verdict, setVerdict] = useState<string | null>(null);
  const [chars, setChars] = useState(0);
  const [previewing, setPreviewing] = useState(false);

  const load = useCallback(async () => {
    try {
      setState(await fetchPersona());
      setError(null);
    } catch (e) {
      setError(e);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const runPreview = useCallback(async () => {
    setPreviewing(true);
    try {
      const r = await previewPersona(probe);
      setPrompt(r.system_prompt);
      setVerdict(r.guardrail.verdict);
      setChars(r.chars);
    } catch (e) {
      setError(e);
    } finally {
      setPreviewing(false);
    }
  }, [probe]);

  if (error && !state) {
    return <ApiErrorPanel error={error} onRetry={load} />;
  }

  if (!state) {
    return <div className="card animate-pulse h-40" />;
  }

  return (
    <div className="space-y-4 page-enter">
      {Boolean(error) && (
        <ApiErrorPanel error={error} onRetry={load} compact>
          <span className="text-xs text-lingxi-muted ml-2">
            显示的是上一次成功加载的人格配置
          </span>
        </ApiErrorPanel>
      )}
      <div className="card">
        <h3 className="text-sm font-medium text-white mb-3">身份定义</h3>
        <dl className="grid grid-cols-1 md:grid-cols-4 gap-4 text-xs">
          <div>
            <dt className="text-lingxi-muted mb-1">名称</dt>
            <dd className="text-white text-sm">{state.identity.name}</dd>
          </div>
          <div>
            <dt className="text-lingxi-muted mb-1">角色</dt>
            <dd className="text-white text-sm">{state.identity.role}</dd>
          </div>
          <div className="md:col-span-2">
            <dt className="text-lingxi-muted mb-1">说话风格</dt>
            <dd className="text-white text-sm">{state.identity.speaking_style}</dd>
          </div>
        </dl>
        {state.identity.backstory && (
          <p className="text-xs text-lingxi-muted mt-3 leading-relaxed border-t border-white/5 pt-3">
            {state.identity.backstory}
          </p>
        )}
      </div>

      <div className="card">
        <h3 className="text-sm font-medium text-white mb-1">上下文模块</h3>
        <p className="text-xs text-lingxi-muted mb-3">
          运行时生效 {state.active_modules.length} /{" "}
          {Object.keys(state.modules).filter((k) => k !== "debug").length} 个
        </p>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
          {Object.entries(state.modules)
            .filter(([k]) => k !== "debug")
            .map(([key, on]) => (
              <div
                key={key}
                className={`px-2.5 py-2 rounded-lg border text-xs transition-colors ${
                  on
                    ? "border-lingxi-green/25 bg-lingxi-green/10 text-lingxi-green"
                    : "border-white/5 bg-white/[0.02] text-lingxi-muted"
                }`}
              >
                <div className="flex items-center gap-1.5">
                  <span
                    className={`w-1.5 h-1.5 rounded-full ${
                      on ? "bg-lingxi-green" : "bg-lingxi-muted/50"
                    }`}
                  />
                  {MODULE_LABELS[key] ?? key}
                </div>
                <p className="text-[10px] opacity-60 mt-0.5">{key}</p>
              </div>
            ))}
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="card">
          <h3 className="text-sm font-medium text-white mb-1">关键词规则</h3>
          <p className="text-xs text-lingxi-muted mb-3">
            已加载 {state.keyword_rules.length} 条 KeywordRule
          </p>
          {state.keyword_rules.length === 0 ? (
            <p className="text-xs text-lingxi-muted">未配置关键词规则</p>
          ) : (
            <ul className="space-y-2">
              {state.keyword_rules.map((r, i) => (
                <li
                  key={i}
                  className="text-xs border-l border-lingxi-orange/30 pl-3 py-1 animate-slide-in"
                >
                  <span className="text-lingxi-orange">
                    {ACTION_LABELS[r.action] ?? r.action}
                  </span>
                  <span className="text-lingxi-muted"> · </span>
                  {r.keywords.map((k) => (
                    <span
                      key={k}
                      className="inline-block px-1.5 py-0.5 mr-1 rounded bg-white/5 text-white/70"
                    >
                      {k}
                    </span>
                  ))}
                  {r.payload && (
                    <p className="text-lingxi-muted mt-1">{r.payload}</p>
                  )}
                </li>
              ))}
            </ul>
          )}
          {state.custom_prompt_keys.length > 0 && (
            <p className="text-xs text-lingxi-muted mt-3 pt-3 border-t border-white/5">
              自定义提示词区块：{state.custom_prompt_keys.join("、")}
            </p>
          )}
        </div>

        <div className="card">
          <h3 className="text-sm font-medium text-white mb-1">注入预览</h3>
          <p className="text-xs text-lingxi-muted mb-3">
            输入一条用户消息，实时查看真正会送给 LLM 的 system prompt 与护栏判定
          </p>
          <div className="flex gap-2">
            <input
              value={probe}
              onChange={(e) => setProbe(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && runPreview()}
              placeholder="例如：我要退货"
              className="flex-1 bg-[#0a0f1e] border border-white/10 rounded-lg px-3 py-2 text-xs text-white/90 outline-none focus:border-lingxi-orange/40"
            />
            <button
              onClick={runPreview}
              disabled={previewing || !probe.trim()}
              className="px-3 py-2 rounded-lg text-xs border border-lingxi-orange/30 bg-lingxi-orange/10 text-lingxi-orange hover:bg-lingxi-orange/20 disabled:opacity-40 transition-colors"
            >
              {previewing ? "生成中…" : "预览"}
            </button>
          </div>

          {verdict && (
            <div className="flex items-center gap-2 mt-3">
              <span
                className={`px-2 py-0.5 rounded text-xs border ${
                  VERDICT_STYLE[verdict] ?? ""
                }`}
              >
                护栏：{verdict}
              </span>
              <span className="text-xs text-lingxi-muted tabular">{chars} 字符</span>
            </div>
          )}

          {prompt !== null && (
            <pre className="mt-3 max-h-64 overflow-auto bg-[#0a0f1e] border border-white/5 rounded-lg p-3 text-[11px] leading-relaxed text-white/70 whitespace-pre-wrap break-words font-mono animate-fade-up">
              {prompt}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}