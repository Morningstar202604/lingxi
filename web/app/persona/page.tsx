"use client";

import { useCallback, useEffect, useState } from "react";
import { fetchSessions } from "@/lib/api";

interface Identity {
  name: string;
  role: string;
  speaking_style: string;
}

interface PersonaState {
  identity: Identity;
  activeModules: string[];
  keywordRulesCount: number;
  totalSessions: number;
}

export default function PersonaPage() {
  const [state, setState] = useState<PersonaState | null>(null);
  const [mode, setMode] = useState<"view" | "edit">("view");
  const [editJson, setEditJson] = useState("");
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const sessions = await fetchSessions();
      const identity: Identity = {
        name: "智犀",
        role: "全栈数字助理",
        speaking_style: "简洁、专业、带一点温暖",
      };
      const activeModules = [
        "time_awareness",
        "turn_stage_awareness",
        "keyword_triggers",
        "emotion_vector",
        "memory_injection",
        "lorebook_injection",
        "variance",
        "fixed_signals",
        "static_rules",
      ];
      setState({
        identity,
        activeModules,
        keywordRulesCount: 0,
        totalSessions: sessions.length,
      });
      setMsg(null);
    } catch (e) {
      setMsg((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const toggleMode = () => {
    if (mode === "view" && state) {
      setEditJson(JSON.stringify(state, null, 2));
    }
    setMode(mode === "view" ? "edit" : "view");
  };

  const handleSave = () => {
    setSaving(true);
    setTimeout(() => {
      setSaving(false);
      setMsg(
        "Persona 配置保存需后端 /api/persona PUT（待实现）。请在 YAML 中修改后重启。",
      );
      setMode("view");
    }, 500);
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="text-xs text-lingxi-muted">
          {state ? `会话总数: ${state.totalSessions}` : "加载中..."}
        </div>
        <button
          onClick={toggleMode}
          className="px-3 py-1.5 rounded-lg text-xs border border-white/10 bg-white/5 text-white/70 hover:bg-white/10 transition-colors"
        >
          {mode === "view" ? "编辑" : "取消"}
        </button>
      </div>

      {msg && (
        <div className="card border-yellow-500/30 text-yellow-400 text-sm py-3 px-4">
          {msg}
        </div>
      )}

      {mode === "view" && state ? (
        <div className="space-y-4">
          <div className="card">
            <h3 className="text-sm font-medium text-white mb-3">身份定义</h3>
            <dl className="grid grid-cols-3 gap-4 text-xs">
              <div>
                <dt className="text-lingxi-muted mb-1">名称</dt>
                <dd className="text-white">{state.identity.name}</dd>
              </div>
              <div>
                <dt className="text-lingxi-muted mb-1">角色</dt>
                <dd className="text-white">{state.identity.role}</dd>
              </div>
              <div>
                <dt className="text-lingxi-muted mb-1">说话风格</dt>
                <dd className="text-white">{state.identity.speaking_style}</dd>
              </div>
            </dl>
          </div>

          <div className="card">
            <h3 className="text-sm font-medium text-white mb-3">模块开关</h3>
            <div className="flex flex-wrap gap-2">
              {state.activeModules.map((m) => (
                <span
                  key={m}
                  className="px-2 py-1 rounded text-xs bg-lingxi-green/10 text-lingxi-green border border-lingxi-green/25"
                >
                  {m}
                </span>
              ))}
            </div>
          </div>

          <div className="card">
            <h3 className="text-sm font-medium text-white mb-3">关键词规则</h3>
            <p className="text-xs text-lingxi-muted">
              已加载 {state.keywordRulesCount} 条 KeywordRule（通过 YAML 配置）
            </p>
          </div>

          <div className="card">
            <h3 className="text-sm font-medium text-white mb-3">
              集成 SSE 事件
            </h3>
            <p className="text-xs text-lingxi-muted">
              升级/认领/关闭事件通过 Server-Sent Events 实时推送（/api/events,
              sse-starlette）
            </p>
          </div>
        </div>
      ) : (
        <div className="space-y-3">
          <p className="text-xs text-lingxi-muted">
            Persona JSON 编辑 — 修改后需 PUT /api/persona（待实现）
          </p>
          <textarea
            value={editJson}
            onChange={(e) => setEditJson(e.target.value)}
            className="w-full h-96 bg-[#0a0f1e] border border-white/10 rounded-lg p-3 text-xs font-mono text-white/80 resize-none"
            spellCheck={false}
          />
          <button
            onClick={handleSave}
            disabled={saving}
            className="px-4 py-2 rounded-lg text-xs border border-lingxi-green/30 bg-lingxi-green/10 text-lingxi-green hover:bg-lingxi-green/20 disabled:opacity-40"
          >
            {saving ? "保存中..." : "保存 (需后端 API)"}
          </button>
        </div>
      )}
    </div>
  );
}
