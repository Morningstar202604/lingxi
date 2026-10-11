"use client";

import { useCallback, useEffect, useState } from "react";
import {
  createExperiment,
  deleteExperiment,
  fetchExperiments,
  updateExperimentStatus,
  type Experiment,
} from "@/lib/api";

const STATUS_LABELS: Record<string, string> = {
  draft: "草稿",
  running: "运行中",
  paused: "已暂停",
  completed: "已完成",
};

const STATUS_COLORS: Record<string, string> = {
  draft: "bg-gray-500/20 text-gray-300",
  running: "bg-lingxi-green/20 text-lingxi-green",
  paused: "bg-yellow-500/20 text-yellow-300",
  completed: "bg-blue-500/20 text-blue-300",
};

export default function ExperimentsPage() {
  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState({
    name: "",
    description: "",
    metric: "resolution_rate",
    minSample: 30,
  });

  const load = useCallback(async () => {
    try {
      const data = await fetchExperiments();
      setExperiments(data);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleCreate = async () => {
    if (!form.name.trim()) return;
    try {
      await createExperiment({
        name: form.name.trim(),
        description: form.description,
        target_metric: form.metric,
        min_sample_size: form.minSample,
        variants: [
          { name: "baseline", variant_type: "baseline", weight: 50 },
          { name: "challenger", variant_type: "challenger", weight: 50 },
        ],
      });
      setShowCreate(false);
      setForm({ name: "", description: "", metric: "resolution_rate", minSample: 30 });
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const handleStatusChange = async (name: string, status: string) => {
    try {
      await updateExperimentStatus(name, status);
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const handleDelete = async (name: string) => {
    if (!confirm(`确定删除实验「${name}」？此操作不可恢复。`)) return;
    try {
      await deleteExperiment(name);
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (error && experiments.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-20">
        <div className="card border-lingxi-red/30 max-w-md text-center">
          <p className="text-lingxi-red text-sm font-medium">无法连接后端</p>
          <p className="text-xs text-lingxi-muted mt-2">{error}</p>
          <button
            onClick={load}
            className="mt-4 px-4 py-2 bg-lingxi-orange/10 text-lingxi-orange border border-lingxi-orange/30 rounded-lg text-xs hover:bg-lingxi-orange/20 transition-colors"
          >
            重试
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-sm font-medium text-white">A/B 实验</h3>
          <p className="text-xs text-lingxi-muted mt-0.5">
            流量分流 · 指标对比 · 自动胜率判定
          </p>
        </div>
        <button
          onClick={() => setShowCreate(!showCreate)}
          className="px-3 py-1.5 bg-lingxi-orange/10 text-lingxi-orange border border-lingxi-orange/30 rounded-lg text-xs hover:bg-lingxi-orange/20 transition-colors"
        >
          {showCreate ? "取消" : "+ 新建实验"}
        </button>
      </div>

      {showCreate && (
        <div className="card space-y-3">
          <h4 className="text-xs font-medium text-white/80">创建新实验</h4>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <label className="text-xs text-lingxi-muted block mb-1">实验名称</label>
              <input
                type="text"
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="如: greeting_v2"
                className="w-full bg-black/30 border border-white/10 rounded-lg px-3 py-1.5 text-xs text-white placeholder-white/30 focus:border-lingxi-orange/50 focus:outline-none"
              />
            </div>
            <div>
              <label className="text-xs text-lingxi-muted block mb-1">描述</label>
              <input
                type="text"
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="实验目的..."
                className="w-full bg-black/30 border border-white/10 rounded-lg px-3 py-1.5 text-xs text-white placeholder-white/30 focus:border-lingxi-orange/50 focus:outline-none"
              />
            </div>
            <div>
              <label className="text-xs text-lingxi-muted block mb-1">目标指标</label>
              <select
                value={form.metric}
                onChange={(e) => setForm({ ...form, metric: e.target.value })}
                className="w-full bg-black/30 border border-white/10 rounded-lg px-3 py-1.5 text-xs text-white focus:border-lingxi-orange/50 focus:outline-none"
              >
                <option value="resolution_rate">解决率</option>
                <option value="escalation_rate">升级率</option>
                <option value="csat">用户满意度</option>
              </select>
            </div>
            <div>
              <label className="text-xs text-lingxi-muted block mb-1">最小样本量 / 组</label>
              <input
                type="number"
                value={form.minSample}
                onChange={(e) => setForm({ ...form, minSample: Number(e.target.value) })}
                min={5}
                className="w-full bg-black/30 border border-white/10 rounded-lg px-3 py-1.5 text-xs text-white focus:border-lingxi-orange/50 focus:outline-none"
              />
            </div>
          </div>
          <button
            onClick={handleCreate}
            disabled={!form.name.trim()}
            className="px-4 py-1.5 bg-lingxi-orange text-white rounded-lg text-xs font-medium hover:bg-lingxi-orange/90 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            创建
          </button>
        </div>
      )}

      {experiments.length === 0 ? (
        <div className="card text-center py-12">
          <p className="text-sm text-lingxi-muted">暂无实验</p>
          <p className="text-xs text-lingxi-muted/60 mt-1">
            点击「+ 新建实验」创建第一个 A/B 实验
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {experiments.map((exp) => (
            <div key={exp.name} className="card">
              <div className="flex items-start justify-between mb-3">
                <div>
                  <div className="flex items-center gap-2">
                    <h4 className="text-sm font-medium text-white">{exp.name}</h4>
                    <span className={`text-[10px] px-1.5 py-0.5 rounded ${STATUS_COLORS[exp.status] || "bg-gray-500/20 text-gray-300"}`}>
                      {STATUS_LABELS[exp.status] || exp.status}
                    </span>
                  </div>
                  {exp.description && (
                    <p className="text-xs text-lingxi-muted mt-0.5">{exp.description}</p>
                  )}
                </div>
                <div className="flex items-center gap-1.5">
                  {exp.status === "draft" && (
                    <button
                      onClick={() => handleStatusChange(exp.name, "running")}
                      className="px-2 py-1 bg-lingxi-green/10 text-lingxi-green border border-lingxi-green/20 rounded text-[10px] hover:bg-lingxi-green/20 transition-colors"
                    >
                      启动
                    </button>
                  )}
                  {exp.status === "running" && (
                    <button
                      onClick={() => handleStatusChange(exp.name, "paused")}
                      className="px-2 py-1 bg-yellow-500/10 text-yellow-300 border border-yellow-500/20 rounded text-[10px] hover:bg-yellow-500/20 transition-colors"
                    >
                      暂停
                    </button>
                  )}
                  {exp.status === "paused" && (
                    <button
                      onClick={() => handleStatusChange(exp.name, "running")}
                      className="px-2 py-1 bg-lingxi-green/10 text-lingxi-green border border-lingxi-green/20 rounded text-[10px] hover:bg-lingxi-green/20 transition-colors"
                    >
                      恢复
                    </button>
                  )}
                  <button
                    onClick={() => handleDelete(exp.name)}
                    className="px-2 py-1 bg-lingxi-red/10 text-lingxi-red/70 border border-lingxi-red/20 rounded text-[10px] hover:bg-lingxi-red/20 transition-colors"
                  >
                    删除
                  </button>
                </div>
              </div>

              <div className="grid grid-cols-2 md:grid-cols-3 gap-2 text-xs">
                <div className="bg-black/20 rounded-lg p-2">
                  <span className="text-lingxi-muted">变体数</span>
                  <p className="text-white font-medium">{exp.variants.length}</p>
                </div>
                <div className="bg-black/20 rounded-lg p-2">
                  <span className="text-lingxi-muted">目标指标</span>
                  <p className="text-white font-medium">{exp.target_metric}</p>
                </div>
                <div className="bg-black/20 rounded-lg p-2">
                  <span className="text-lingxi-muted">最小样本 / 组</span>
                  <p className="text-white font-medium">{exp.min_sample_size}</p>
                </div>
              </div>

              <div className="mt-2 text-xs text-lingxi-muted">
                变体: {exp.variants.map((v) => `${v.name} (${v.weight}%)`).join(" · ")}
              </div>
            </div>
          ))}
        </div>
      )}

      {error && experiments.length > 0 && (
        <div className="card border-lingxi-red/30 text-center">
          <p className="text-lingxi-red text-xs">{error}</p>
          <button onClick={load} className="mt-2 text-xs text-lingxi-orange hover:underline">
            重试
          </button>
        </div>
      )}
    </div>
  );
}
