"use client";

import { useCallback, useEffect, useState } from "react";
import {
  addRule,
  deleteRule,
  evaluateRules,
  fetchRules,
  fetchSlaStatus,
  type AutomationRule,
  type SlaStatus,
} from "@/lib/api";
import ApiErrorPanel from "@/components/ApiState";

/**
 * 自动化规则 + SLA 页（rules 端点与 /api/sla/status 此前控制台零消费）。
 *
 * 规则引擎在消息链路里真实生效（server 侧 evaluate → escalate/inject）；
 * 这里提供规则的查看/新增/删除与"试评估"（不执行，只看命中）。
 */

export default function RulesPage() {
  const [rules, setRules] = useState<AutomationRule[]>([]);
  const [sla, setSla] = useState<SlaStatus | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState("");
  const [testMessage, setTestMessage] = useState("");
  const [testResult, setTestResult] = useState<unknown[] | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [newRule, setNewRule] = useState({
    name: "",
    trigger: "keyword",
    keywords: "",
    action: "escalate",
    reason: "",
    priority: 50,
  });

  const load = useCallback(async () => {
    try {
      const list = await fetchRules();
      setRules(list);
      setError(null);
    } catch (e) {
      setError(e);
    }
  }, []);

  const loadSla = useCallback(async () => {
    try {
      setSla(await fetchSlaStatus());
    } catch {
      /* SLA 监控未挂载（sla_monitor=None）时不显示即可 */
    }
  }, []);

  useEffect(() => {
    load();
    loadSla();
    const id = setInterval(loadSla, 15_000);
    return () => clearInterval(id);
  }, [load, loadSla]);

  const handleAdd = useCallback(async () => {
    if (!newRule.name.trim()) return;
    setBusy("add");
    try {
      // 字段名贴合后端 Rule/RuleCondition/RuleAction 模型：
      // conditions[].field 用 "message_contains"，actions 是数组
      await addRule({
        name: newRule.name.trim(),
        enabled: true,
        conditions: newRule.keywords
          ? [
              {
                field: "message_contains",
                operator: "contains",
                value: newRule.keywords.trim(),
              },
            ]
          : [],
        actions: [
          {
            action: newRule.action,
            params: newRule.reason ? { reason: newRule.reason } : {},
          },
        ],
        priority: newRule.priority,
      });
      setNewRule({
        name: "",
        trigger: "keyword",
        keywords: "",
        action: "escalate",
        reason: "",
        priority: 50,
      });
      setShowAdd(false);
      await load();
    } catch (e) {
      setError(e);
    } finally {
      setBusy("");
    }
  }, [newRule, load]);

  const handleDelete = useCallback(
    async (name: string) => {
      setBusy(name);
      try {
        await deleteRule(name);
        await load();
      } catch (e) {
        setError(e);
      } finally {
        setBusy("");
      }
    },
    [load],
  );

  const handleEvaluate = useCallback(async () => {
    if (!testMessage.trim()) return;
    setBusy("eval");
    try {
      // 后端返回 {"matched_actions": [{action, params}]}
      const r = await evaluateRules({ message: testMessage.trim(), confidence: 1.0 });
      setTestResult(r.matched_actions as unknown[]);
    } catch (e) {
      setError(e);
    } finally {
      setBusy("");
    }
  }, [testMessage]);

  if (error && rules.length === 0) {
    return <ApiErrorPanel error={error} onRetry={load} />;
  }

  return (
    <div className="space-y-6">
      {/* ── SLA 概览 ── */}
      {sla && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div className="card">
            <div className="text-xs text-lingxi-muted">监控工单</div>
            <div className="text-2xl font-bold text-white mt-1">{sla.summary.total}</div>
          </div>
          <div className="card">
            <div className="text-xs text-lingxi-muted">合规</div>
            <div className="text-2xl font-bold text-lingxi-green mt-1">
              {sla.summary.compliant}
            </div>
          </div>
          <div className="card">
            <div className="text-xs text-lingxi-muted">违规</div>
            <div
              className={`text-2xl font-bold mt-1 ${
                sla.summary.breached > 0 ? "text-lingxi-red" : "text-white"
              }`}
            >
              {sla.summary.breached}
            </div>
          </div>
          <div className="card">
            <div className="text-xs text-lingxi-muted">违规率</div>
            <div
              className={`text-2xl font-bold mt-1 ${
                sla.summary.breach_rate > 0.1 ? "text-lingxi-red" : "text-lingxi-green"
              }`}
            >
              {(sla.summary.breach_rate * 100).toFixed(1)}%
            </div>
          </div>
        </div>
      )}

      {/* ── 试评估 ── */}
      <div className="card">
        <div className="text-sm font-semibold text-white mb-3">规则试评估（不执行）</div>
        <div className="flex gap-3">
          <input
            value={testMessage}
            onChange={(e) => setTestMessage(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleEvaluate()}
            placeholder="输入一条用户消息，看会命中哪些规则…"
            className="flex-1 px-3 py-2 rounded-lg bg-black/30 border border-white/10 text-xs text-white focus:border-lingxi-orange/50 focus:outline-none"
          />
          <button
            onClick={handleEvaluate}
            disabled={busy === "eval" || !testMessage.trim()}
            className="px-4 py-2 rounded-lg text-xs border border-lingxi-orange/30 bg-lingxi-orange/10 text-lingxi-orange hover:bg-lingxi-orange/20 transition-colors disabled:opacity-40"
          >
            {busy === "eval" ? "评估中..." : "评估"}
          </button>
        </div>
        {testResult !== null && (
          <div className="mt-3">
            {testResult.length === 0 ? (
              <div className="text-xs text-lingxi-muted">没有规则命中</div>
            ) : (
              <pre className="p-3 rounded-lg bg-black/30 border border-white/5 text-[11px] text-white/80 overflow-x-auto">
                {JSON.stringify(testResult, null, 2)}
              </pre>
            )}
          </div>
        )}
      </div>

      {/* ── 规则列表 ── */}
      <div className="flex items-center justify-between">
        <div className="text-xs text-lingxi-muted">
          共 {rules.length} 条规则 · 优先级数字越小越先匹配
        </div>
        <button
          onClick={() => setShowAdd((v) => !v)}
          className="px-3 py-1.5 rounded-lg text-xs border border-white/10 text-white/70 hover:bg-white/5 transition-colors"
        >
          {showAdd ? "取消" : "新增规则"}
        </button>
      </div>

      {showAdd && (
        <div className="card space-y-3">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <div>
              <div className="text-[10px] text-lingxi-muted mb-1">规则名</div>
              <input
                value={newRule.name}
                onChange={(e) => setNewRule({ ...newRule, name: e.target.value })}
                placeholder="refund_keyword"
                className="w-full px-3 py-2 rounded-lg bg-black/30 border border-white/10 text-xs text-white focus:border-lingxi-orange/50 focus:outline-none"
              />
            </div>
            <div>
              <div className="text-[10px] text-lingxi-muted mb-1">关键词包含</div>
              <input
                value={newRule.keywords}
                onChange={(e) => setNewRule({ ...newRule, keywords: e.target.value })}
                placeholder="退款没到账"
                className="w-full px-3 py-2 rounded-lg bg-black/30 border border-white/10 text-xs text-white focus:border-lingxi-orange/50 focus:outline-none"
              />
            </div>
            <div>
              <div className="text-[10px] text-lingxi-muted mb-1">动作</div>
              <select
                value={newRule.action}
                onChange={(e) => setNewRule({ ...newRule, action: e.target.value })}
                className="w-full px-3 py-2 rounded-lg bg-black/30 border border-white/10 text-xs text-white focus:border-lingxi-orange/50 focus:outline-none"
              >
                <option value="escalate">升级人工</option>
                <option value="inject_prompt">注入提示</option>
              </select>
            </div>
            <div>
              <div className="text-[10px] text-lingxi-muted mb-1">优先级</div>
              <input
                type="number"
                value={newRule.priority}
                onChange={(e) =>
                  setNewRule({ ...newRule, priority: Number(e.target.value) || 0 })
                }
                className="w-full px-3 py-2 rounded-lg bg-black/30 border border-white/10 text-xs text-white focus:border-lingxi-orange/50 focus:outline-none"
              />
            </div>
          </div>
          <button
            onClick={handleAdd}
            disabled={busy === "add" || !newRule.name.trim()}
            className="px-4 py-2 rounded-lg text-xs border border-lingxi-green/30 bg-lingxi-green/10 text-lingxi-green hover:bg-lingxi-green/20 transition-colors disabled:opacity-40"
          >
            {busy === "add" ? "添加中..." : "添加"}
          </button>
        </div>
      )}

      <div className="space-y-2">
        {rules.length === 0 ? (
          <div className="card text-xs text-lingxi-muted">规则引擎无规则</div>
        ) : (
          rules.map((r, i) => (
            <div key={r.name ?? i} className="card flex items-start justify-between gap-4">
              <div className="min-w-0">
                <div className="text-sm text-white/90">{r.name ?? `规则 ${i + 1}`}</div>
                <pre className="mt-2 text-[11px] text-lingxi-muted overflow-x-auto">
                  {JSON.stringify(r, null, 2)}
                </pre>
              </div>
              {r.name && (
                <button
                  onClick={() => r.name && handleDelete(r.name)}
                  disabled={busy === r.name}
                  className="shrink-0 px-3 py-1.5 rounded-lg text-[11px] border border-lingxi-red/30 text-lingxi-red hover:bg-lingxi-red/10 transition-colors disabled:opacity-40"
                >
                  {busy === r.name ? "..." : "删除"}
                </button>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
