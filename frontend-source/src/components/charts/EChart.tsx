import React, { useEffect, useRef } from 'react';
import * as echarts from 'echarts/core';
import {
  BarChart, FunnelChart, GaugeChart, GraphChart, HeatmapChart, LineChart,
  PieChart, RadarChart, SankeyChart, ScatterChart, TreeChart,
} from 'echarts/charts';
import {
  GridComponent,
  LegendComponent,
  TooltipComponent,
  VisualMapComponent,
} from 'echarts/components';
import { LabelLayout } from 'echarts/features';
import { CanvasRenderer } from 'echarts/renderers';

/**
 * ECharts 按需注册（task 5.1）
 *
 * 只注册本平台用到的图表与组件，避免引入全量包体积。
 * 新增图表类型时在此补充注册即可。
 */
echarts.use([
  LineChart,
  BarChart,
  PieChart,
  HeatmapChart,
  ScatterChart,
  RadarChart,
  SankeyChart,
  GraphChart,
  TreeChart,
  FunnelChart,
  GaugeChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  VisualMapComponent,
  LabelLayout,
  CanvasRenderer,
]);

/**
 * 3D 矩阵坐标系（echarts-gl）按需懒加载：
 * echarts-gl 体积大且仅 3D 图需要，动态 import 使其成为独立 chunk，
 * 页面未出现 3D 图时不会加载。echarts-gl 在 import 时自注册全部 GL 图表。
 */
let glReady: Promise<unknown> | null = null;
function ensureGL(): Promise<unknown> {
  if (!glReady) {
    glReady = import('echarts-gl');
  }
  return glReady;
}

/** option 是否需要 GL（含 3D 坐标轴） */
function optionNeedsGL(option: Record<string, unknown>): boolean {
  return 'xAxis3D' in option || 'yAxis3D' in option || 'zAxis3D' in option;
}

export interface EChartProps {
  /** ECharts option（由后端格式转换层输出拼装） */
  option: Record<string, unknown>;
  height?: number | string;
  loading?: boolean;
  /** true 时整体替换 option（避免残留旧系列） */
  notMerge?: boolean;
}

const EChart: React.FC<EChartProps> = ({
  option,
  height = 280,
  loading = false,
  notMerge = false,
}) => {
  const hostRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ReturnType<typeof echarts.init> | null>(null);

  // 初始化/销毁：容器尺寸变化时自适应
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return undefined;
    const chart = echarts.init(host);
    chartRef.current = chart;
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(host);
    return () => {
      observer.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (optionNeedsGL(option)) {
        try {
          await ensureGL();
        } catch (e) {
          // GL 加载失败不阻塞其余图表
          console.error('echarts-gl 加载失败', e);
          return;
        }
      }
      if (cancelled) return;
      const chart = chartRef.current;
      if (!chart) return;
      try {
        chart.setOption(option, notMerge);
      } catch {
        // GL 首渲染与 React StrictMode 双挂载 dispose 竞态会损坏实例状态
        // （仅 dev 出现，生产无双重挂载）：销毁重建后再试一次
        try { chart.dispose(); } catch { /* 已释放 */ }
        if (cancelled || !hostRef.current) return;
        const fresh = echarts.init(hostRef.current);
        chartRef.current = fresh;
        try {
          fresh.setOption(option, notMerge);
        } catch (e) {
          console.error('EChart setOption 失败（已重试）', e);
        }
      }
    })();
    return () => { cancelled = true; };
  }, [option, notMerge]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    if (loading) {
      chart.showLoading('default', { text: '加载中…', color: '#2383E2' });
    } else {
      chart.hideLoading();
    }
  }, [loading]);

  return <div ref={hostRef} style={{ width: '100%', height }} />;
};

export default EChart;
