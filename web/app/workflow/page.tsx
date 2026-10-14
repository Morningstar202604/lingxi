"use client";

import { useCallback, useEffect, useState } from "react";
import {
  executeWorkflow,
  fetchWorkflowGraph,
  fetchWorkflowYaml,
  previewWorkflowGraph,
} from "@/lib/api";
import ApiErrorPanel from "@/components/ApiState";

/**
 * 工作流 DAG 页（/api/workflow/* 四个端点此前控制台零消费）。
 *
 * DAG 是 pipeline.handle_event 链的可执行镜像：这里能看默认流程图与 YAML、
 * 预校验修改后的 YAML（拿到新 Mermaid 预览），并能对当前默认 DAG 触发一次
 * 真实执行（组件缺失时 executor 会按设计优雅降级为 skipped）。
 */

export default function WorkflowPage() {
  const [mermaid, setMermaid] = useState("");
  const [yamlText, setYamlText] = useState("");
  const [draftYaml, setDraftYaml] = useState("");
  const [preview, setPreview] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState("");
  const [execResult, setExecResult] = useState<string>("");
  const [tab, setTab] = useState<"graph" | "yaml">("graph");

  const load = useCallback(async () => {
    try {
      const [m, y] = await Promise.all([fetchWorkflowGraph(), fetchWorkflowYaml()]);
      setMermaid(m);
      setYamlText(y);
      setDraftYaml((prev) => (prev ? prev : y));
      setError(null);
    } catch (e) {
      setError(e);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handlePreview = useCallback(async () => {
    if (!draftYaml.trim()) return;
    setBusy("preview");
    try {
      const m = await previewWorkflowGraph(draftYaml);
      setPreview(m);
    } catch (e) {
      setError(e);
      setPreview("");
    } finally {
      setBusy("");
    }
  }, [draftYaml]);

  const handleExecute = useCallback(async () => {
    setBusy("execute");
    setExecResult("");
    try {
      // 后端契约：body 需要 message（必填）+ 可选 user_id/chat_id/channel
      const result = await executeWorkflow({
        message: "工作流页测试执行",
        user_id: "dashboard",
        chat_id: "dashboard",
        channel: "qq_private",
      });
      setExecResult(JSON.stringify(result, null, 2));
    } catch (e) {
      setError(e);
    } finally {
      setBusy("");
    }
  }, []);

  if (error && !mermaid) {
    return <ApiErrorPanel error={error} onRetry={load} />;
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex gap-1">
          <button
            onClick={() => setTab("graph")}
            className={`px-3 py-1.5 rounded-lg text-xs transition-colors ${
              tab === "graph"
                ? "bg-lingxi-orange/10 text-lingxi-orange"
                : "text-white/60 hover:bg-white/5"
            }`}
          >
            流程图
          </button>
          <button
            onClick={() => setTab("yaml")}
            className={`px-3 py-1.5 rounded-lg text-xs transition-colors ${
              tab === "yaml"
                ? "bg-lingxi-orange/10 text-lingxi-orange"
                : "text-white/60 hover:bg-white/5"
            }`}
          >
            YAML 编辑
          </button>
        </div>
        <button
          onClick={handleExecute}
          disabled={busy === "execute"}
          className="px-4 py-2 rounded-lg text-xs border border-lingxi-green/30 bg-lingxi-green/10 text-lingxi-green hover:bg-lingxi-green/20 transition-colors disabled:opacity-40"
        >
          {busy === "execute" ? "执行中..." : "执行默认 DAG"}
        </button>
      </div>

      <div className="text-xs text-lingxi-muted leading-relaxed">
        默认 DAG 描述的是消息处理管线（intake → rules → agent → escalate/respond）。
        Mermaid 源码可直接复制到 mermaid.live 等任意渲染器查看；
        YAML 可修改后点「预览」校验并生成新流程图。
      </div>

      {tab === "graph" && (
        <div className="card">
          <div className="text-sm font-semibold text-white mb-2">默认流程图（Mermaid）</div>
          <pre className="p-3 rounded-lg bg-black/30 border border-white/5 text-[11px] text-white/80 overflow-x-auto max-h-[480px] overflow-y-auto">
            {mermaid || "加载中..."}
          </pre>
        </div>
      )}

      {tab === "yaml" && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div className="card">
            <div className="flex items-center justify-between mb-2">
              <div className="text-sm font-semibold text-white">YAML 定义</div>
              <button
                onClick={handlePreview}
                disabled={busy === "preview"}
                className="px-3 py-1.5 rounded-lg text-[11px] border border-lingxi-orange/30 text-lingxi-orange hover:bg-lingxi-orange/10 transition-colors disabled:opacity-40"
              >
                {busy === "preview" ? "校验中..." : "校验并预览"}
              </button>
            </div>
            <textarea
              value={draftYaml}
              onChange={(e) => setDraftYaml(e.target.value)}
              spellCheck={false}
              className="w-full h-[420px] p-3 rounded-lg bg-black/30 border border-white/10 text-[11px] text-white/80 font-mono focus:border-lingxi-orange/50 focus:outline-none resize-none"
            />
          </div>
          <div className="card">
            <div className="text-sm font-semibold text-white mb-2">
              预览（校验通过后）
            </div>
            <pre className="p-3 rounded-lg bg-black/30 border border-white/5 text-[11px] text-white/80 overflow-x-auto h-[420px] overflow-y-auto">
              {preview || "点左侧「校验并预览」生成"}
            </pre>
          </div>
        </div>
      )}

      {execResult && (
        <div className="card">
          <div className="text-sm font-semibold text-white mb-2">执行结果</div>
          <pre className="p-3 rounded-lg bg-black/30 border border-white/5 text-[11px] text-white/80 overflow-x-auto max-h-[360px] overflow-y-auto">
            {execResult}
          </pre>
        </div>
      )}
    </div>
  );
}
