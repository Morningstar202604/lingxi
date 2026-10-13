"use client";

import { useCallback, useEffect, useState } from "react";
import {
  deleteKnowledgeDoc,
  fetchKnowledgeDocs,
  fetchKnowledgeStats,
  recallKnowledge,
  resetKnowledge,
  uploadKnowledge,
  type KnowledgeDoc,
  type KnowledgeStats,
  type RecallHit,
} from "@/lib/api";
import ApiErrorPanel from "@/components/ApiState";

export default function KnowledgePage() {
  const [stats, setStats] = useState<KnowledgeStats | null>(null);
  const [docs, setDocs] = useState<KnowledgeDoc[]>([]);
  const [content, setContent] = useState("");
  const [source, setSource] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadMsg, setUploadMsg] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [recalling, setRecalling] = useState(false);
  const [hits, setHits] = useState<RecallHit[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [resetting, setResetting] = useState(false);

  const loadStats = useCallback(async () => {
    try {
      const data = await fetchKnowledgeStats();
      setStats(data);
    } catch (e) {
      setError(e);
    }
  }, []);

  const loadDocs = useCallback(async () => {
    try {
      const data = await fetchKnowledgeDocs();
      setDocs(data);
    } catch (e) {
      setError(e);
    }
  }, []);

  useEffect(() => {
    loadStats();
    loadDocs();
  }, [loadStats, loadDocs]);

  const loadAll = useCallback(
    () => Promise.all([loadStats(), loadDocs()]),
    [loadStats, loadDocs],
  );

  const handleUpload = async () => {
    if (!content.trim()) {
      setUploadMsg("文档内容不能为空");
      return;
    }
    setUploading(true);
    setUploadMsg(null);
    try {
      const result = await uploadKnowledge(content, "", source);
      setUploadMsg(
        `上传成功: ${result.chunk_count} chunks (doc_id: ${result.doc_id.slice(0, 12)})`,
      );
      setContent("");
      setSource("");
      await Promise.all([loadStats(), loadDocs()]);
    } catch (e) {
      setUploadMsg(`上传失败: ${(e as Error).message}`);
    } finally {
      setUploading(false);
    }
  };

  const handleRecall = async () => {
    if (!query.trim()) return;
    setRecalling(true);
    try {
      const results = await recallKnowledge(query);
      setHits(results);
    } catch (e) {
      setError(e);
    } finally {
      setRecalling(false);
    }
  };

  const handleDelete = async (docId: string) => {
    try {
      await deleteKnowledgeDoc(docId);
      await Promise.all([loadStats(), loadDocs()]);
    } catch (e) {
      setError(e);
    }
  };

  const handleReset = async () => {
    if (!confirm("确认清空知识库? 此操作不可恢复。")) return;
    setResetting(true);
    try {
      await resetKnowledge();
      setHits([]);
      await Promise.all([loadStats(), loadDocs()]);
    } catch (e) {
      setError(e);
    } finally {
      setResetting(false);
    }
  };

  return (
    <div className="space-y-6">
      {Boolean(error) && (
        <ApiErrorPanel error={error} onRetry={loadAll} compact />
      )}

      {/* Stats Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="card">
          <div className="text-xs text-lingxi-muted mb-1">总 Chunks 数</div>
          <div className="text-2xl font-bold text-white">
            {stats ? stats.count : "—"}
          </div>
        </div>
        <div className="card">
          <div className="text-xs text-lingxi-muted mb-1">文档数</div>
          <div className="text-2xl font-bold text-white">{docs.length}</div>
        </div>
        <div className="card">
          <div className="text-xs text-lingxi-muted mb-1">向量库名</div>
          <div className="text-lg font-mono text-white">
            {stats ? stats.name : "—"}
          </div>
        </div>
      </div>

      {/* Upload Section */}
      <div className="card">
        <h3 className="text-sm font-medium text-white mb-3">上传文档</h3>
        <textarea
          value={content}
          onChange={(e) => setContent(e.target.value)}
          placeholder="在此粘贴文档内容..."
          className="w-full h-40 bg-[#0a0f1e] border border-white/10 rounded-lg p-3 text-xs font-mono text-white/80 resize-none mb-3"
          spellCheck={false}
        />
        <div className="flex items-center gap-3">
          <input
            type="text"
            value={source}
            onChange={(e) => setSource(e.target.value)}
            placeholder="来源标识 (可选)"
            className="flex-1 bg-[#0a0f1e] border border-white/10 rounded-lg px-3 py-2 text-xs text-white/80"
          />
          <button
            onClick={handleUpload}
            disabled={uploading}
            className="px-4 py-2 rounded-lg text-xs border border-lingxi-green/30 bg-lingxi-green/10 text-lingxi-green hover:bg-lingxi-green/20 disabled:opacity-40 transition-colors"
          >
            {uploading ? "上传中..." : "上传"}
          </button>
        </div>
        {uploadMsg && (
          <p className="mt-2 text-xs text-lingxi-muted">{uploadMsg}</p>
        )}
      </div>

      {/* Recall Section */}
      <div className="card">
        <h3 className="text-sm font-medium text-white mb-3">召回测试</h3>
        <div className="flex items-center gap-3 mb-4">
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="输入查询..."
            className="flex-1 bg-[#0a0f1e] border border-white/10 rounded-lg px-3 py-2 text-xs text-white/80"
            onKeyDown={(e) => {
              if (e.key === "Enter") handleRecall();
            }}
          />
          <button
            onClick={handleRecall}
            disabled={recalling}
            className="px-4 py-2 rounded-lg text-xs border border-lingxi-orange/30 bg-lingxi-orange/10 text-lingxi-orange hover:bg-lingxi-orange/20 disabled:opacity-40 transition-colors"
          >
            {recalling ? "召回中..." : "召回"}
          </button>
        </div>
        {hits.length > 0 ? (
          <div className="space-y-3">
            {hits.map((hit, i) => (
              <div
                key={`${hit.doc_id}-${hit.chunk_index}-${i}`}
                className="border border-white/5 rounded-lg p-3 bg-white/[0.02]"
              >
                <div className="flex items-center justify-between mb-2">
                  <span className="text-[10px] text-lingxi-muted font-mono">
                    [{i + 1}] source: {hit.source} | chunk: {hit.chunk_index}
                  </span>
                  <span className="text-[10px] text-lingxi-orange">
                    score: {hit.score}
                  </span>
                </div>
                <p className="text-xs text-white/70 leading-relaxed">
                  {hit.text}
                </p>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-xs text-lingxi-muted">
            {recalling ? "召回中..." : "无召回结果"}
          </p>
        )}
      </div>

      {/* Document Management */}
      <div className="card">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-medium text-white">文档管理</h3>
          <button
            onClick={handleReset}
            disabled={resetting}
            className="px-3 py-1.5 rounded-lg text-xs border border-lingxi-red/30 text-lingxi-red hover:bg-lingxi-red/10 disabled:opacity-40 transition-colors"
          >
            {resetting ? "清空中..." : "清空知识库"}
          </button>
        </div>
        {docs.length === 0 ? (
          <p className="text-xs text-lingxi-muted">暂无文档</p>
        ) : (
          <div className="space-y-2">
            {docs.map((doc) => (
              <div
                key={doc.doc_id}
                className="flex items-center justify-between border border-white/5 rounded-lg px-3 py-2 bg-white/[0.02]"
              >
                <div className="flex-1 min-w-0">
                  <div className="text-xs font-mono text-white/80 truncate">
                    {doc.doc_id}
                  </div>
                  <div className="text-[10px] text-lingxi-muted truncate">
                    {doc.source}
                  </div>
                </div>
                <button
                  onClick={() => handleDelete(doc.doc_id)}
                  className="ml-3 px-2 py-1 rounded text-[10px] border border-lingxi-red/20 text-lingxi-red hover:bg-lingxi-red/10 transition-colors"
                >
                  删除
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
