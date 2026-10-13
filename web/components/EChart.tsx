"use client";

// ── ECharts 集成壳 ──
// 为什么自己写这层壳而不是直接 import echarts：
//   1) echarts 全量包 ~1MB(gzip ~330KB)。这里用 echarts/core + 按需注册图表，
//      只带 Line/Pie/Gauge/Bar + 必要组件，实测降到 ~330KB(gzip ~110KB)。
//   2) echarts-for-react 这类封装只省几行样板，却带来 React 版本耦合；
//      官方文档给的就是"init + setOption + dispose"这三步，直接照做更可控。
//   3) 实例必须在 useEffect 里建/销：Next.js App Router 会 SSR，
//      DOM 不存在时 echarts.init() 会抛错。

import { useEffect, useRef } from "react";

import { BarChart, GaugeChart, LineChart, PieChart } from "echarts/charts";
import {
  GraphicComponent,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
} from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";

// GraphicComponent 不可省：StatusDonut 用 `graphic` 在圆心画"总数"。
// 少注册它不会让构建或类型检查报错，只在浏览器控制台报
// "[ECharts] Component graphic is used but not imported"，然后圆心数字不显示。
echarts.use([
  LineChart,
  PieChart,
  GaugeChart,
  BarChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  GraphicComponent,
  CanvasRenderer,
]);

export interface EChartProps {
  option: echarts.EChartsCoreOption;
  height?: number;
  /** 依赖变化时是否重置动画（数据整体替换时设 false 更平滑） */
  notMerge?: boolean;
}

export default function EChart({ option, height = 260, notMerge = false }: EChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = echarts.init(containerRef.current, undefined, {
      renderer: "canvas",
    });
    chartRef.current = chart;

    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(containerRef.current);

    return () => {
      observer.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    // notMerge=true：轮询数据整体替换时不该残留上一次的 series
    chartRef.current?.setOption(option, notMerge);
  }, [option, notMerge]);

  return <div ref={containerRef} style={{ height, width: "100%" }} />;
}