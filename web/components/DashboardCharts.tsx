"use client";

// ── 控制台动态可视化 ──
// 数据来源：/api/dashboard/summary 每 5 秒轮询一次的真实值。
// 趋势线是**客户端滚动采样**（保留最近 60 个点 ≈ 5 分钟）——
// 后端目前不存时序历史，页面不会伪造"历史数据"，只画本次会话观测到的。

import { useMemo } from "react";

import EChart from "@/components/EChart";
import type { EChartsCoreOption } from "echarts/core";

const AXIS = "#95A5A6";
const SPLIT = "rgba(255,255,255,0.06)";

const baseTooltip = {
  trigger: "axis" as const,
  backgroundColor: "rgba(21,32,58,0.94)",
  borderColor: "rgba(232,150,62,0.35)",
  textStyle: { color: "#e2e8f0", fontSize: 11 },
};

const gridBase = { left: 34, right: 14, top: 22, bottom: 24 };

export interface TrendPoint {
  t: number;
  active: number;
  escalated: number;
  queuePending: number;
}

export interface StatusSlice {
  name: string;
  value: number;
  color: string;
}

/** 活跃/升级/队列随时间的走势 */
export function TrendChart({ points }: { points: TrendPoint[] }) {
  const option = useMemo<EChartsCoreOption>(() => {
    const labels = points.map((p) =>
      new Date(p.t).toLocaleTimeString("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      }),
    );
    const line = (name: string, data: number[], color: string) => ({
      name,
      type: "line" as const,
      data,
      smooth: true,
      showSymbol: false,
      symbolSize: 6,
      lineStyle: { width: 2, color },
      itemStyle: { color },
      areaStyle: {
        color: {
          type: "linear" as const,
          x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [
            { offset: 0, color: `${color}44` },
            { offset: 1, color: `${color}00` },
          ],
        },
      },
    });
    return {
      tooltip: baseTooltip,
      legend: {
        textStyle: { color: AXIS, fontSize: 11 },
        itemWidth: 10,
        itemHeight: 6,
        top: 0,
      },
      grid: gridBase,
      xAxis: {
        type: "category",
        boundaryGap: false,
        data: labels,
        axisLine: { lineStyle: { color: SPLIT } },
        axisLabel: {
          color: AXIS,
          fontSize: 10,
          // 只留首/尾/中点，避免 60 个刻度糊成一片
          interval: Math.max(1, Math.floor(labels.length / 5)),
        },
      },
      yAxis: {
        type: "value",
        minInterval: 1,
        axisLine: { show: false },
        splitLine: { lineStyle: { color: SPLIT } },
        axisLabel: { color: AXIS, fontSize: 10 },
      },
      series: [
        line("活跃工单", points.map((p) => p.active), "#5B8DEF"),
        line("已升级", points.map((p) => p.escalated), "#E74C3C"),
        line("队列待分配", points.map((p) => p.queuePending), "#E8963E"),
      ],
    };
  }, [points]);

  return (
    <div className="card">
      <p className="text-xs text-lingxi-muted mb-1">实时趋势（最近采样）</p>
      <EChart option={option} height={230} />
    </div>
  );
}

/** 工单状态构成 */
export function StatusDonut({ slices }: { slices: StatusSlice[] }) {
  const option = useMemo<EChartsCoreOption>(() => {
    const total = slices.reduce((a, s) => a + s.value, 0);
    return {
      tooltip: {
        ...baseTooltip,
        trigger: "item" as const,
        formatter: "{b}: {c} ({d}%)",
      },
      legend: {
        orient: "vertical" as const,
        right: 0,
        top: "center",
        textStyle: { color: AXIS, fontSize: 11 },
        itemWidth: 9,
        itemHeight: 9,
      },
      series: [
        {
          type: "pie",
          radius: ["52%", "78%"],
          center: ["38%", "50%"],
          avoidLabelOverlap: true,
          itemStyle: {
            borderColor: "#1B2A4A",
            borderWidth: 2,
            borderRadius: 4,
          },
          label: { show: false },
          emphasis: {
            scale: true,
            scaleSize: 6,
            label: { show: false },
          },
          data: slices.map((s) => ({
            name: s.name,
            value: s.value,
            itemStyle: { color: s.color },
          })),
        },
      ],
      graphic: total
        ? [
            {
              type: "text",
              left: "34%",
              top: "44%",
              style: {
                text: String(total),
                fill: "#e2e8f0",
                fontSize: 26,
                fontWeight: 700,
                textAlign: "center",
              },
            },
            {
              type: "text",
              left: "32%",
              top: "62%",
              style: {
                text: "活跃",
                fill: AXIS,
                fontSize: 11,
                textAlign: "center",
              },
            },
          ]
        : [],
    };
  }, [slices]);

  return (
    <div className="card">
      <p className="text-xs text-lingxi-muted mb-1">工单状态构成</p>
      <EChart option={option} height={230} />
    </div>
  );
}

/** 最长等待 vs SLA 红线 */
export function WaitGauge({ seconds, threshold = 120 }: { seconds: number; threshold?: number }) {
  const option = useMemo<EChartsCoreOption>(() => {
    const over = seconds > threshold;
    const color = over ? "#E74C3C" : "#2ECC71";
    return {
      series: [
        {
          type: "gauge",
          startAngle: 210,
          endAngle: -30,
          min: 0,
          max: Math.max(threshold * 2, seconds * 1.2, 1),
          radius: "92%",
          center: ["50%", "56%"],
          progress: {
            show: true,
            width: 10,
            roundCap: true,
            itemStyle: { color },
          },
          axisLine: {
            lineStyle: { width: 10, color: [[1, "rgba(255,255,255,0.08)"]] },
          },
          pointer: { show: false },
          axisTick: { show: false },
          splitLine: { show: false },
          axisLabel: { show: false },
          anchor: { show: false },
          title: {
            offsetCenter: [0, "32%"],
            color: AXIS,
            fontSize: 11,
          },
          detail: {
            offsetCenter: [0, "0%"],
            formatter: "{value} 秒",
            color: "#e2e8f0",
            fontSize: 22,
            fontWeight: 700,
          },
          data: [
            {
              value: Math.round(seconds),
              name: over ? `超过 SLA ${threshold}s` : "等待正常",
            },
          ],
        },
      ],
    };
  }, [seconds, threshold]);

  return (
    <div className="card">
      <p className="text-xs text-lingxi-muted mb-1">队列最长等待</p>
      <EChart option={option} height={230} />
    </div>
  );
}