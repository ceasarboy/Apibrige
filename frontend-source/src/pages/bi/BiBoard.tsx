import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, Empty, Input, InputNumber, message, Modal, Select, Space, Spin, Table, Tag } from 'antd';
import { ExpandOutlined, PlusOutlined } from '@ant-design/icons';
import apiClient from '../../api/client';
import EChart from '../../components/charts/EChart';
import { DataCard, type Tone } from '../../components/screen/ScreenDecor';

/** 后端返回的聚合指标定义 */
export interface BiMetricDef {
  metric_key: string;
  name: string;
  description?: string | null;
  source_type: string;
  filters?: Record<string, unknown> | Array<Record<string, unknown>> | null;
  source_ref?: string | null;
  granularity: string;
  time_field?: string;
  dimensions: Array<Record<string, unknown>>;
  measures: Array<Record<string, unknown>>;
  chart_type: string;
  top_n: number;
  enabled: boolean;
  /** 图表级样式：number=bg_color/font_color；gauge=gauge_min/gauge_max */
  style?: Record<string, unknown> | null;
  builtin: boolean;
}

/** 图表类型 → 后端格式参数 */
export const FORMAT_BY_CHART: Record<string, string> = {
  line: 'echarts-line',
  bar: 'echarts-bar',
  pie: 'echarts-pie',
  heatmap: 'echarts-heatmap',
  table: 'table',
  number: 'datav-number',
  detail: 'bi-detail',
  // 2026-09-25 图表库扩充
  scatter: 'echarts-scatter',
  radar: 'echarts-radar',
  sankey: 'echarts-sankey',
  graph: 'echarts-graph',
  flow: 'echarts-flow',
  tree: 'echarts-tree',
  funnel: 'echarts-funnel',
  gauge: 'echarts-gauge',
  time_line: 'echarts-line',
  bar3d: 'echarts-bar3d',
};

export const PERIOD_OPTIONS = [
  { label: '最近 1 小时', value: '1h' },
  { label: '最近 6 小时', value: '6h' },
  { label: '最近 24 小时', value: '24h' },
  { label: '最近 7 天', value: '7d' },
];

const PERIOD_HOURS: Record<string, number> = { '1h': 1, '6h': 6, '24h': 24, '7d': 24 * 7 };

/** 时间范围 → ISO 区间（喂给 /bi/metrics/{key}） */
export function periodToRange(period: string): { from: string; to: string } {
  const hours = PERIOD_HOURS[period] ?? 24;
  const to = new Date();
  const from = new Date(to.getTime() - hours * 3600_000);
  return { from: from.toISOString(), to: to.toISOString() };
}

// ============================================================
// ECharts option 拼装
// ============================================================

const AXIS_COLOR_DARK = '#7fa6cc';
const AXIS_COLOR_LIGHT = '#9B9A97';
const SPLIT_COLOR_DARK = 'rgba(64,158,255,0.12)';
const SPLIT_COLOR_LIGHT = '#F1F0EF';

export function buildOption(
  chartType: string,
  data: any,
  dark = false,
  styleCfg: Record<string, any> = {},
  unit = '',
): Record<string, unknown> {
  const axisColor = dark ? AXIS_COLOR_DARK : AXIS_COLOR_LIGHT;
  const splitColor = dark ? SPLIT_COLOR_DARK : SPLIT_COLOR_LIGHT;
  const palette = dark
    ? ['#2fc1ff', '#27d17c', '#ffb020', '#ff5c5c', '#9d7bff', '#00d0c0']
    : ['#2383E2', '#0F7B4B', '#D9730D', '#E03E3E', '#7B61FF', '#00A6A6'];

  if (chartType === 'pie') {
    return {
      color: palette,
      tooltip: { trigger: 'item' },
      legend: { bottom: 0, textStyle: { color: axisColor, fontSize: 11 } },
      series: (data?.series ?? []).map((s: any) => ({
        ...s,
        // 小占比扇区的引导标签易互相叠压，隐藏重叠项（图例仍可见）
        labelLayout: { hideOverlap: true },
        label: { fontSize: 10 },
      })),
    };
  }

  if (chartType === 'heatmap') {
    // visualMap 不设 max 时 ECharts 默认按 0-100 映射，小数值会落在最浅端
    // 与底色几乎同色（实测热力格不可见）——按数据动态取 max
    const cellVals = (data?.series ?? []).flatMap((s: any) =>
      (s.data ?? []).map((d: any) => Number(d?.[2] ?? 0)));
    const vMax = Math.max(1, ...cellVals);
    return {
      color: palette,
      tooltip: { position: 'top', formatter: (p: any) =>
        `${p.name ?? ''}<br/>${p.value?.[1] ?? ''}：${p.value?.[2] ?? ''}` },
      grid: { left: 56, right: 16, top: 16, bottom: 40 },
      xAxis: { ...(data?.xAxis ?? {}), axisLabel: { color: axisColor, fontSize: 10 } },
      yAxis: { ...(data?.yAxis ?? {}), axisLabel: { color: axisColor, fontSize: 10 } },
      visualMap: {
        min: 0,
        max: vMax,
        show: false,
        calculable: false,
        inRange: { color: dark ? ['#0b2b4a', '#2fc1ff'] : ['#EAF3FC', '#2383E2'] },
      },
      series: data?.series ?? [],
    };
  }

  if (chartType === 'scatter') {
    return {
      color: palette,
      tooltip: { trigger: 'item' },
      legend: { top: 0, textStyle: { color: axisColor, fontSize: 11 } },
      grid: { left: 52, right: 16, top: 32, bottom: 28 },
      xAxis: { type: 'value', axisLabel: { color: axisColor, fontSize: 10 },
               splitLine: { lineStyle: { color: splitColor } } },
      yAxis: { type: 'value', axisLabel: { color: axisColor, fontSize: 10 },
               splitLine: { lineStyle: { color: splitColor } } },
      series: (data?.series ?? []).map((s: any) => ({
        name: s.name, type: 'scatter', data: s.data, symbolSize: 10,
      })),
    };
  }

  if (chartType === 'radar') {
    return {
      color: palette,
      tooltip: {},
      legend: { top: 0, textStyle: { color: axisColor, fontSize: 11 } },
      radar: {
        indicator: (data?.indicators ?? []).map((i: any) => ({ name: i.name })),
        axisName: { color: axisColor, fontSize: 10 },
        splitLine: { lineStyle: { color: splitColor } },
        splitArea: { show: false },
        axisLine: { lineStyle: { color: splitColor } },
      },
      series: [{
        type: 'radar',
        data: (data?.series ?? []).map((s: any) => ({
          name: s.name, value: s.values, areaStyle: { opacity: 0.15 },
        })),
      }],
    };
  }

  if (chartType === 'sankey') {
    return {
      color: palette,
      tooltip: { trigger: 'item' },
      series: [{
        type: 'sankey', left: 20, right: 100, top: 10, bottom: 10,
        data: data?.nodes ?? [], links: data?.links ?? [],
        nodeGap: 10, nodeWidth: 14,
        emphasis: { focus: 'adjacency' },
        lineStyle: { color: 'gradient', opacity: 0.35, curveness: 0.5 },
        label: { color: axisColor, fontSize: 10 },
      }],
    };
  }

  if (chartType === 'graph' || chartType === 'flow') {
    return {
      color: palette,
      tooltip: {},
      series: [{
        type: 'graph', layout: 'force', roam: true,
        data: data?.nodes ?? [], links: data?.links ?? [],
        edgeSymbol: chartType === 'flow' ? ['none', 'arrow'] : ['none', 'none'],
        edgeSymbolSize: 9,
        force: { repulsion: 260, edgeLength: 100, gravity: 0.08 },
        label: { show: true, color: axisColor, fontSize: 10 },
        lineStyle: {
          color: 'source', opacity: 0.6,
          width: chartType === 'flow' ? 2 : 1,
          curveness: chartType === 'flow' ? 0.25 : 0.05,
        },
        emphasis: { focus: 'adjacency' },
      }],
    };
  }

  if (chartType === 'tree') {
    return {
      color: palette,
      tooltip: { trigger: 'item', triggerOn: 'mousemove' },
      series: [{
        type: 'tree', data: [data?.tree ?? { name: '—', children: [] }],
        left: 24, right: 130, top: 10, bottom: 10,
        orient: 'LR', roam: true,
        label: { show: true, color: axisColor, fontSize: 10 },
        emphasis: { focus: 'descendant' },
      }],
    };
  }

  if (chartType === 'funnel') {
    return {
      color: palette,
      tooltip: { trigger: 'item', formatter: '{b}: {c}' },
      series: [{
        type: 'funnel', left: 40, right: 40, top: 12, bottom: 12,
        sort: 'descending', gap: 2,
        data: data?.items ?? [],
        label: { show: true, position: 'inside', color: '#fff', fontSize: 11 },
      }],
    };
  }

  if (chartType === 'gauge') {
    const min = Number(styleCfg?.gauge_min ?? 0);
    const max = Number(styleCfg?.gauge_max ?? 100);
    const raw = data?.value;
    return {
      series: [{
        type: 'gauge', min, max, startAngle: 210, endAngle: -30,
        progress: { show: true, width: 10 },
        axisLine: { lineStyle: { width: 10,
          color: [[1, dark ? 'rgba(64,158,255,0.15)' : '#eee']] } },
        axisTick: { show: false },
        splitLine: { length: 6, lineStyle: { color: axisColor } },
        axisLabel: { color: axisColor, fontSize: 9, distance: 14 },
        pointer: { width: 4, itemStyle: { color: dark ? '#2fc1ff' : '#2383E2' } },
        detail: {
          valueAnimation: true, fontSize: 22, offsetCenter: [0, '62%'],
          color: dark ? '#dfe9f5' : '#333',
          formatter: (v: any) => (typeof v === 'number'
            ? `${Math.round(v * 100) / 100}${unit}` : '—'),
        },
        data: [{ value: typeof raw === 'number' ? raw : 0, name: '' }],
      }],
    };
  }

  if (chartType === 'bar3d') {
    return {
      tooltip: {},
      visualMap: {
        show: false, min: 0,
        max: Math.max(1, ...(data?.cells ?? []).map((c: any) => Number(c[2] ?? 0))),
        inRange: { color: dark ? ['#0b2b4a', '#2fc1ff'] : ['#EAF3FC', '#2383E2'] },
      },
      xAxis3D: { type: 'category', data: data?.x ?? [], name: '' },
      yAxis3D: { type: 'category', data: data?.y ?? [], name: '' },
      zAxis3D: { type: 'value' },
      grid3D: {
        boxWidth: 110, boxDepth: 90, boxHeight: 70,
        viewControl: { distance: 240, autoRotate: false },
        axisLabel: { textStyle: { color: axisColor, fontSize: 9 } },
        axisLine: { lineStyle: { color: splitColor } },
        splitLine: { lineStyle: { color: splitColor } },
        light: { main: { intensity: 1.3 }, ambient: { intensity: 0.35 } },
      },
      series: [{
        type: 'bar3D', shading: 'lambert',
        data: (data?.cells ?? []).map((c: any) => ({ value: [c[0], c[1], c[2]] })),
        barSize: 6,
      }],
    };
  }

  // line / bar / time_line（时间坐标系：x 轴 type=time）
  const isTime = chartType === 'time_line';
  return {
    color: palette,
    tooltip: { trigger: 'axis' },
    legend: { top: 0, textStyle: { color: axisColor, fontSize: 11 } },
    grid: { left: 52, right: 16, top: 32, bottom: 28 },
    xAxis: {
      ...(data?.xAxis ?? {}),
      ...(isTime ? { type: 'time' } : {}),
      axisLabel: { color: axisColor, fontSize: 10, hideOverlap: true },
      axisLine: { lineStyle: { color: splitColor } },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: axisColor, fontSize: 10 },
      splitLine: { lineStyle: { color: splitColor } },
    },
    series: (data?.series ?? []).map((s: any) => ({
      ...s,
      type: chartType === 'bar' ? 'bar' : 'line',
      smooth: chartType !== 'bar',
      areaStyle: chartType !== 'bar' && (data?.series ?? []).length === 1
        ? { opacity: 0.12 }
        : undefined,
    })),
  };
}

// ============================================================
// 数据加载
// ============================================================

export interface MetricPayload {
  metricKey: string;
  chartType: string;
  data: any;
}

export function useMetricsData(
  defs: BiMetricDef[],
  period: string,
  refreshKey: number,
  dimFilters?: DimFilterSelection,
): { loading: boolean; payloads: Record<string, MetricPayload> } {
  const [loading, setLoading] = useState(false);
  const [payloads, setPayloads] = useState<Record<string, MetricPayload>>({});

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      if (defs.length === 0) {
        setPayloads({});
        return;
      }
      setLoading(true);
      const { from, to } = periodToRange(period);
      const results = await Promise.all(defs.map(async (def) => {
        // 明细卡走独立的服务端分页数据流（DetailTable 自取 /bi/form-rows），
        // 不再经指标聚合接口拉全量（add-bi-detail-query）
        if (def.chart_type === 'detail') {
          return [def.metric_key, {
            metricKey: def.metric_key, chartType: def.chart_type, data: {},
          }] as const;
        }
        const format = FORMAT_BY_CHART[def.chart_type] ?? 'raw';
        // 维度筛选只对该卡**自己拥有**的维度名下发：
        // - 无分组图表（KPI/仪表盘）不参与（否则被误杀成空）；
        // - 分组维度名不同的卡（如「车间」分组 vs 「产线」筛选）也不下发——
        //   桶按自己的维度组合存，下发不匹配的维度名会把该卡整卡筛空（实测）。
        const params: Record<string, unknown> = { from, to, format };
        const ownDims = new Set((def.dimensions ?? []).map((d: any) => String(d?.name ?? '')));
        if (ownDims.size > 0) {
          const dimEntries = Object.entries(dimFilters ?? {})
            .filter(([k, v]) => v !== undefined && v !== '' && ownDims.has(k))
            .map(([k, v]) => `${k}:${v}`);
          if (dimEntries.length) params.dim = dimEntries;
        }
        try {
          const res = await apiClient.get(`/bi/metrics/${def.metric_key}`, {
            params,
            paramsSerializer: { indexes: null },
          });
          return [def.metric_key, {
            metricKey: def.metric_key,
            chartType: def.chart_type,
            data: res.data?.data,
          }] as const;
        } catch {
          // 拦截器已提示；这里保持该项为空，不影响其他图表
          return [def.metric_key, {
            metricKey: def.metric_key,
            chartType: def.chart_type,
            data: null,
          }] as const;
        }
      }));
      if (!cancelled) {
        setPayloads(Object.fromEntries(results));
        setLoading(false);
      }
    };
    load();
    return () => { cancelled = true; };
  }, [defs, period, refreshKey, dimFilters]);

  return { loading, payloads };
}

// ============================================================
// 单指标渲染
// ============================================================

/** 深色简易表格（大屏投放用，antd Table 在深色底上对比度差）
 *  全部行渲染 + 卡内滚动（scr-table height:100% overflow:auto，表头吸顶） */
const DarkTable: React.FC<{ columns: any[]; rows: any[] }> = ({ columns, rows }) => (
  <div className="scr-table">
    <div className="scr-table__head">
      {columns.map((c) => <span key={c.dataIndex}>{c.title}</span>)}
    </div>
    {rows.map((r, i) => (
      <div className="scr-table__row" key={i}>
        {columns.map((c) => <span key={c.dataIndex}>{String(r[c.dataIndex] ?? '—')}</span>)}
      </div>
    ))}
  </div>
);

// ============================================================
// 表单明细表（detail）：列标题来自 /bi/form-fields 元数据（path 对齐）
// ============================================================

interface FormFieldMeta {
  path: string;
  name: string;
  type: string;
  bi_type: 'dimension' | 'number' | 'time';
}

const formFieldsCache = new Map<string, Promise<FormFieldMeta[]>>();

function loadFormFields(connId: string, formKey: string): Promise<FormFieldMeta[]> {
  const key = `${connId}:${formKey}`;
  if (!formFieldsCache.has(key)) {
    formFieldsCache.set(key, apiClient
      .get('/bi/form-fields', { params: { connection_id: connId, form_key: formKey } })
      .then((r) => r.data?.fields ?? [])
      .catch(() => []));
  }
  return formFieldsCache.get(key)!;
}

export function parseFormRef(sourceRef?: string | null):
  { connectionId: string; formKey: string } {
  try {
    const cfg = JSON.parse(sourceRef ?? '{}');
    return {
      connectionId: String(cfg.connection_id ?? ''),
      formKey: String(cfg.form_key ?? ''),
    };
  } catch {
    return { connectionId: '', formKey: '' };
  }
}

const DetailTable: React.FC<{
  def: BiMetricDef;
  payload?: MetricPayload;
  loading?: boolean;
  dark?: boolean;
  height?: number | string;
  /** 投放态自动轮播滚动：内容溢出时恒速上滚，到底停留后回顶循环（悬停暂停） */
  autoScroll?: boolean;
  /** 弹层内渲染（隐藏放大按钮，避免递归） */
  inModal?: boolean;
}> = ({ def, dark = false, autoScroll = false, inModal = false }) => {
  const { connectionId, formKey } = parseFormRef(def.source_ref);
  const [columns, setColumns] = useState<{ title: string; dataIndex: string }[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [paused, setPaused] = useState(false);
  const [expanded, setExpanded] = useState(false);
  // ---- 服务端分页状态（add-bi-detail-query：明细大表逐行分析） ----
  const PAGE_SIZE = 200;
  const [page, setPage] = useState(1);
  const [keyword, setKeyword] = useState('');
  const [debouncedKw, setDebouncedKw] = useState('');
  const [sortField, setSortField] = useState('');
  const [order, setOrder] = useState<'asc' | 'desc'>('asc');
  const [loadingRows, setLoadingRows] = useState(false);
  const [server, setServer] = useState<{
    rows: any[]; hasMore: boolean; total: number | null; fallback: string | null;
  }>({ rows: [], hasMore: false, total: null, fallback: null });

  useEffect(() => {
    let cancelled = false;
    if (!connectionId || !formKey) return undefined;
    loadFormFields(connectionId, formKey).then((metas) => {
      if (cancelled) return;
      setColumns(metas.map((f) => ({ title: f.name, dataIndex: f.path })));
    });
    return () => { cancelled = true; };
  }, [connectionId, formKey]);

  // 关键字防抖 + 条件变化回到第一页
  useEffect(() => {
    const t = setTimeout(() => setDebouncedKw(keyword), 300);
    return () => clearTimeout(t);
  }, [keyword]);
  useEffect(() => { setPage(1); }, [debouncedKw, sortField, order]);

  // 分页拉取（DB 源分页/排序下推；HAP/降级路径服务端内存切片）
  useEffect(() => {
    let cancelled = false;
    if (!connectionId || !formKey) return undefined;
    setLoadingRows(true);
    apiClient.get('/bi/form-rows', {
      params: {
        connection_id: connectionId, form_key: formKey,
        page, page_size: PAGE_SIZE,
        keyword: debouncedKw || undefined,
        sort_field: sortField || undefined, order,
      },
    }).then((r) => {
      if (cancelled) return;
      const d = r.data ?? {};
      setServer({
        rows: d.rows ?? [], hasMore: Boolean(d.has_more),
        total: d.total ?? null, fallback: d.fallback ?? null,
      });
    }).catch(() => {
      if (!cancelled) setServer({ rows: [], hasMore: false, total: null, fallback: null });
    }).finally(() => { if (!cancelled) setLoadingRows(false); });
    return () => { cancelled = true; };
  }, [connectionId, formKey, page, debouncedKw, sortField, order]);

  // 自动轮播：rAF 恒速上滚，到底停留 DWELL 后回顶循环；悬停暂停
  useEffect(() => {
    if (!autoScroll || paused) return undefined;
    const el = scrollRef.current;
    if (!el) return undefined;
    const SPEED = 22;    // px/s（大屏阅读节奏）
    const DWELL = 1600;  // 到底停留（ms）
    let raf = 0;
    let last = 0;
    let dwellUntil = 0;
    // 本地浮点累积：scrollTop 读回可能被取整，直接 += 小数会永远停在 0
    let pos = el.scrollTop;
    const step = (ts: number) => {
      raf = requestAnimationFrame(step);
      if (!last) { last = ts; return; }
      const dt = (ts - last) / 1000;
      last = ts;
      const overflow = el.scrollHeight - el.clientHeight;
      if (overflow <= 2 || ts < dwellUntil) return;
      pos += SPEED * dt;
      if (pos >= overflow - 1) {
        dwellUntil = ts + DWELL;
        pos = 0;
      }
      el.scrollTop = pos;
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [autoScroll, paused, server.rows.length, page]);

  const rows = server.rows;
  // 列显示名优先级：卡级别名（style.column_labels）> 字段字典 name > 原始字段名
  const columnLabels = ((def.style as Record<string, any> | null | undefined)
    ?.column_labels ?? {}) as Record<string, string>;
  // 元数据缺失时兜底：取首行键当列
  const colsRaw = columns.length > 0 ? columns
    : (rows[0] ? Object.keys(rows[0]).map((k) => ({ title: k, dataIndex: k })) : []);
  const cols = colsRaw.map((c) => ({
    ...c, title: columnLabels[c.dataIndex] ?? c.title,
  }));
  const columnOptions = cols.map((c) => ({ value: c.dataIndex, label: c.title }));

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      {/* 工具条：搜索 + 排序 + 页码 */}
      <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 6,
                    flexWrap: 'wrap' }}>
        <Input.Search size="small" allowClear placeholder="搜索"
                      style={{ width: 150 }}
                      onSearch={setKeyword}
                      onChange={(e) => { if (!e.target.value) setKeyword(''); }} />
        <Select size="small" allowClear placeholder="排序字段" style={{ width: 130 }}
                options={columnOptions} value={sortField || undefined}
                onChange={(v) => setSortField(v ?? '')} />
        <Button size="small" disabled={!sortField}
                onClick={() => setOrder((o) => (o === 'asc' ? 'desc' : 'asc'))}>
          {order === 'asc' ? '↑ 升序' : '↓ 降序'}
        </Button>
        <span style={{ marginLeft: 'auto', fontSize: 11,
                       color: dark ? 'var(--scr-text-dim)' : 'var(--nb-text-secondary)' }}>
          第 {page} 页{server.total != null ? ` · 共 ${server.total} 行` : ''}
          {server.fallback === 'keyword_memory' ? '（搜索为内存过滤）' : ''}
          {server.fallback === 'sort_memory' ? '（降序为内存排序）' : ''}
        </span>
        {!inModal && (
          <Button size="small" type="text" icon={<ExpandOutlined />}
                  title="放大查看（大屏缩放下操作更方便）"
                  style={{ color: dark ? 'var(--scr-text-dim)' : undefined }}
                  onClick={() => setExpanded(true)} />
        )}
      </div>
      <div
        ref={scrollRef}
        data-bi-detail-scroll
        onMouseEnter={() => setPaused(true)}
        onMouseLeave={() => setPaused(false)}
        style={{ flex: 1, minHeight: 0, overflow: 'auto' }}
      >
        {loadingRows && rows.length === 0 ? (
          <div style={{ height: '100%', display: 'flex', alignItems: 'center',
                        justifyContent: 'center' }}>
            <Spin />
          </div>
        ) : rows.length === 0 ? (
          <Empty description="暂无数据" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : dark ? (
          <DarkTable columns={cols} rows={rows} />
        ) : (
          <Table
            size="small"
            rowKey="__key"
            columns={cols.map((c: any) => ({
              title: c.title, dataIndex: c.dataIndex, ellipsis: true,
            }))}
            dataSource={rows.map((r: any, i: number) => ({ ...r, __key: i }))}
            pagination={false}
            scroll={{ x: 'max-content' }}
          />
        )}
      </div>
      {/* 翻页 */}
      <div style={{ display: 'flex', justifyContent: 'center', gap: 8, paddingTop: 4 }}>
        <Button size="small" disabled={page <= 1 || loadingRows}
                onClick={() => setPage((p) => Math.max(1, p - 1))}>上一页</Button>
        <Button size="small" disabled={!server.hasMore || loadingRows}
                onClick={() => setPage((p) => p + 1)}>下一页</Button>
      </div>
      {/* 放大查看：弹层不经画布缩放，控件为正常尺寸（大屏缩放下也可操作） */}
      {!inModal && (
        <Modal open={expanded} onCancel={() => setExpanded(false)} footer={null}
               width="88vw" title={`明细：${def.name}`} destroyOnHidden>
          <div style={{ height: '72vh' }}>
            <DetailTable def={def} dark={false} autoScroll={false} inModal />
          </div>
        </Modal>
      )}
    </div>
  );
};

const formRowsCache = new Map<string, Promise<any[]>>();

/** 表单原始行（筛选栏的维度候选值）——用**分页**端点：DB 源分页下推，
 *  只取一页（≤200 行）；老版不带 page 的全量路径在大表上会触读取上限并 502。
 *  失败静默（候选值缺失不该弹错），并打上 skipErrorMessage 让拦截器不弹提示。 */
export function loadFormRows(connId: string, formKey: string): Promise<any[]> {
  const key = `${connId}:${formKey}`;
  if (!formRowsCache.has(key)) {
    formRowsCache.set(key, apiClient
      .get('/bi/form-rows', {
        params: { connection_id: connId, form_key: formKey, page: 1, page_size: 200 },
        skipErrorMessage: true,
      })
      .then((r) => r.data?.rows ?? [])
      .catch(() => []));
  }
  return formRowsCache.get(key)!;
}

export interface DimFilterSelection { [dimName: string]: string; }

/** 收集启用聚合图表用到的维度（明细/指标卡无分组不参与）
 *
 *  同一维度名可能出现在多张卡、且来自不同源表：候选值取"被引用次数最多"的源表。
 *  不能取第一个——按 key 排序时大表指标可能排在前（实测：产线候选取到了百万行
 *  压测表的 L0-L4），且对大表做候选值读取本身会触到读取上限。
 */
export function collectDimDefs(defs: BiMetricDef[]):
  Array<{ name: string; def: BiMetricDef; field: string }> {
  const usable = defs.filter((d) => d.chart_type !== 'detail' && d.chart_type !== 'number');
  const counts = new Map<string, Map<string, number>>();
  const first = new Map<string, { def: BiMetricDef; field: string }>();
  for (const d of usable) {
    for (const dim of d.dimensions ?? []) {
      const name = String((dim as any).name ?? '');
      const field = String((dim as any).field ?? '');
      if (!name || !field) continue;
      if (!counts.has(name)) counts.set(name, new Map());
      const bySource = counts.get(name)!;
      bySource.set(d.source_ref, (bySource.get(d.source_ref) ?? 0) + 1);
      if (!first.has(name)) first.set(name, { def: d, field });
    }
  }
  const out: Array<{ name: string; def: BiMetricDef; field: string }> = [];
  for (const [name, bySource] of counts.entries()) {
    let best = first.get(name)!;
    let bestCount = bySource.get(best.def.source_ref) ?? 0;
    for (const d of usable) {                       // 按 defs 顺序稳定择源
      const dim = (d.dimensions ?? []).find((x: any) => String(x?.name ?? '') === name) as any;
      if (!dim) continue;
      const count = bySource.get(d.source_ref) ?? 0;
      if (count > bestCount) {
        bestCount = count;
        best = { def: d, field: String(dim.field ?? '') };
      }
    }
    out.push({ name, def: best.def, field: best.field });
  }
  return out;
}

/** 大屏筛选元素（看板设置中添加/移除；候选值自动从表单原始行提取，
 *  选中经 ?dim= 注入全部启用图表——DataEase 运行时注入范式） */
export const DimFilterBar: React.FC<{
  defs: BiMetricDef[];
  /** 看板设置中启用的维度名列表 */
  dimNames: string[];
  dark?: boolean;
  value: DimFilterSelection;
  onChange: (v: DimFilterSelection) => void;
}> = ({ defs, dimNames, dark = false, value, onChange }) => {
  const dimDefs = useMemo(() => {
    const all = collectDimDefs(defs);
    return dimNames
      .map((name) => all.find((d) => d.name === name))
      .filter((d): d is { name: string; def: BiMetricDef; field: string } => !!d);
  }, [defs, dimNames]);

  const [options, setOptions] = useState<Record<string, string[]>>({});

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const next: Record<string, string[]> = {};
      for (const { name, def, field } of dimDefs) {
        if (next[name] !== undefined) continue;
        if (def.source_type === 'form') {
          const { connectionId, formKey } = parseFormRef(def.source_ref);
          if (connectionId && formKey) {
            const rows = await loadFormRows(connectionId, formKey);
            next[name] = [...new Set(rows
              .map((r: any) => String(r[field] ?? '').trim())
              .filter((v: string) => v))].sort();
            continue;
          }
        }
        next[name] = [];
      }
      if (!cancelled) setOptions(next);
    })();
    return () => { cancelled = true; };
  }, [dimDefs]);

  if (dimDefs.length === 0) return null;
  return (
    <div style={{
      display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center',
      marginBottom: 14,
    }}>
      <span style={{
        fontSize: 12, color: dark ? '#9db8d4' : 'var(--nb-text-secondary)',
      }}>
        筛选
      </span>
      {dimDefs.map(({ name }) => (
        <Select
          key={name} allowClear showSearch size="small"
          placeholder={name}
          style={{ minWidth: 150 }}
          value={value[name]}
          options={(options[name] ?? []).map((v) => ({ value: v, label: v }))}
          onChange={(v) => onChange({ ...value, [name]: v ?? '' })}
        />
      ))}
      {Object.values(value).some((v) => v) && (
        <Button size="small" onClick={() => onChange({})}>清空</Button>
      )}
    </div>
  );
};

/** 投放显示设置（DataEase 式：基准分辨率 + 适配模式 + 主题），持久化由调用方负责 */
export const ADAPTOR_OPTIONS = [
  { value: 'keep', label: '等比缩放居中（最通用）' },
  { value: 'widthFirst', label: '宽度撑满·高度滚动（超宽屏/条屏）' },
  { value: 'heightFirst', label: '高度撑满·宽度滚动（竖屏/高屏）' },
  { value: 'full', label: '拉伸填满（强制铺满，比例失真）' },
] as const;

export const BASE_SIZE_PRESETS = [
  { label: '1920 × 1080（16:9）', w: 1920, h: 1080 },
  { label: '1280 × 720（16:9）', w: 1280, h: 720 },
  { label: '2560 × 720（带状屏）', w: 2560, h: 720 },
  { label: '3840 × 2160（4K）', w: 3840, h: 2160 },
];

export const ScreenDisplaySettings: React.FC<{
  settings: ScreenSettings;
  onChange: (s: ScreenSettings) => void;
}> = ({ settings, onChange }) => {
  const baseW = settings.base_w ?? 1920;
  const baseH = settings.base_h ?? 1080;
  const presetMatch = BASE_SIZE_PRESETS.find((p) => p.w === baseW && p.h === baseH);
  const patch = (p: Partial<ScreenSettings>) => onChange({ ...settings, ...p });
  const numInput = (v: number, on: (n: number) => void) => (
    <InputNumber size="small" min={360} max={8192} value={v}
                 onChange={(n) => n && on(n)} style={{ width: 90 }} />
  );
  return (
    <div style={{ width: 300 }}>
      <div style={{
        fontSize: 12, color: 'var(--nb-text-secondary)', marginBottom: 8,
      }}>
        投放显示：不同设备按同一画布坐标缩放呈现（DataEase screenAdaptor 范式）。
      </div>
      <div style={{ marginBottom: 10 }}>
        <div style={{ fontSize: 12, marginBottom: 4 }}>画布比例（基准分辨率）</div>
        <Select size="small" style={{ width: '100%' }}
                value={presetMatch ? `${presetMatch.w}x${presetMatch.h}` : 'custom'}
                options={[
                  ...BASE_SIZE_PRESETS.map((p) => ({
                    value: `${p.w}x${p.h}`, label: p.label,
                  })),
                  { value: 'custom', label: '自定义' },
                ]}
                onChange={(v) => {
                  if (v === 'custom') return;
                  const [w, h] = String(v).split('x').map(Number);
                  patch({ base_w: w, base_h: h });
                }} />
        {presetMatch ? null : (
          <Space size={6} style={{ marginTop: 6 }}>
            {numInput(baseW, (n) => patch({ base_w: n }))}
            <span style={{ fontSize: 12 }}>×</span>
            {numInput(baseH, (n) => patch({ base_h: n }))}
          </Space>
        )}
      </div>
      <div style={{ marginBottom: 10 }}>
        <div style={{ fontSize: 12, marginBottom: 4 }}>适配模式</div>
        <Select size="small" style={{ width: '100%' }}
                value={settings.screen_adaptor ?? 'keep'}
                options={ADAPTOR_OPTIONS.map((o) => ({ value: o.value, label: o.label }))}
                onChange={(v) => patch({ screen_adaptor: v })} />
        <div style={{ fontSize: 11, color: 'var(--nb-text-muted)', marginTop: 4 }}>
          设备比例与画布比例一致时（如 16:9 屏用 1920×1080），各模式效果相同；
          换用非标设备（条屏/竖屏/超宽）才会出现差异。
        </div>
      </div>
      <div>
        <div style={{ fontSize: 12, marginBottom: 4 }}>主题底色</div>
        <Select size="small" style={{ width: '100%' }}
                value={settings.theme ?? 'dark'}
                options={[
                  { value: 'dark', label: '深色底（作战室/车间大屏）' },
                  { value: 'light', label: '白底（明亮环境/打印）' },
                ]}
                onChange={(v) => patch({ theme: v })} />
      </div>
    </div>
  );
};

/** 看板设置：筛选元素管理（可添加/移除；持久化由调用方负责） */
export const ScreenFilterSettings: React.FC<{
  defs: BiMetricDef[];
  selected: string[];
  onChange: (dims: string[]) => void;
}> = ({ defs, selected, onChange }) => {
  const available = collectDimDefs(defs);
  const addable = available.filter((a) => !selected.includes(a.name));
  return (
    <div style={{ width: 280 }}>
      <div style={{
        fontSize: 12, color: 'var(--nb-text-secondary)', marginBottom: 8,
      }}>
        筛选元素：启用后显示在大屏顶部，选中值联动所有含该维度的图表。
      </div>
      <div style={{ marginBottom: 10 }}>
        {selected.length === 0 ? (
          <span style={{ fontSize: 12, color: 'var(--nb-text-muted)' }}>
            尚未添加筛选元素
          </span>
        ) : (
          selected.map((name) => (
            <Tag key={name} closable
                 onClose={() => onChange(selected.filter((s) => s !== name))}>
              {name}
            </Tag>
          ))
        )}
      </div>
      {available.length === 0 ? (
        <div style={{ fontSize: 12, color: 'var(--nb-text-muted)' }}>
          启用的图表尚无分组维度（需在指标定义中配置维度）
        </div>
      ) : (
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {addable.map((a) => (
            <Button key={a.name} size="small" icon={<PlusOutlined />}
                    onClick={() => onChange([...selected, a.name])}>
              {a.name}
            </Button>
          ))}
          {addable.length === 0 && (
            <div style={{ fontSize: 12, color: 'var(--nb-text-muted)' }}>
              已添加全部可用维度
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export interface ScreenLayout { order?: string[]; spans?: Record<string, number>; }

/** 72×36 自由画布的单卡格位（DataEase dashboard 同款网格语义） */
export interface CellRect { x: number; y: number; w: number; h: number; }

export interface ScreenSettings {
  layout?: ScreenLayout;
  /** 自由画布：metric_key → 格位（cols=72 × 36 行）。多看板语义：**在 cells 里即上屏**（摆放即归属） */
  cells?: Record<string, CellRect>;
  filter_dims?: string[];
  /** 看板显示名（大屏标题） */
  name?: string;
  /** 投放画布基准分辨率（DataEase canvasStyleData.width/height 同义），缺省 1920×1080 */
  base_w?: number;
  base_h?: number;
  /** 适配模式（DataEase screenAdaptor 同义）：keep=等比居中 / widthFirst / heightFirst / full */
  screen_adaptor?: 'keep' | 'widthFirst' | 'heightFirst' | 'full';
  /** 主题底色：dark（默认）| light */
  theme?: 'dark' | 'light';
}

/** 画布网格常量（与 DataEase 72×36 对齐） */
export const CANVAS_COLS = 72;
export const CANVAS_ROWS = 36;
export const CANVAS_ROW_HEIGHT = 30;

/**
 * 布局归一：优先 cells；旧 {order,spans} 或无布局迁移为 cells
 * （旧半宽→w=36、全宽→w=72，h=12；order 为空按 defs 顺序紧凑排）
 */
export function resolveCells(
  _defs: BiMetricDef[],
  layout?: ScreenLayout,
  cells?: Record<string, CellRect>,
): Record<string, CellRect> {
  if (cells) return cells;
  if (layout?.order?.length) {
    const out: Record<string, CellRect> = {};
    let col = 0;
    let row = 0;
    for (const key of layout.order) {
      const w = (layout.spans?.[key] === 2 ? 72 : 36);
      if (col + w > CANVAS_COLS) {
        col = 0;
        row += 12;
      }
      out[key] = { x: col, y: row, w, h: 12 };
      col += w;
    }
    return out;
  }
  // 多看板语义「摆放即归属」：无 cells 且无旧布局 = 空看板（不上屏任何卡）
  return {};
}

/** 添加卡片时的默认格位：画布最底行之下靠左放一张半宽卡 */
export function nextFreeCell(cells: Record<string, CellRect>): CellRect | null {
  for (let y = 0; y <= CANVAS_ROWS - 12; y++) {
    for (let x = 0; x <= CANVAS_COLS - 36; x++) {
      const candidate = { x, y, w: 36, h: 12 };
      const overlaps = Object.values(cells).some(c =>
        x < c.x + c.w && x + 36 > c.x && y < c.y + c.h && y + 12 > c.y);
      if (!overlaps) return candidate;
    }
  }
  return null;
}

/** 看板设置（服务端持久化；布局与图表定义分离——DataEase 范式） */
export function useScreenSettings(board: string = 'default', pollMs = 0): {
  settings: ScreenSettings;
  save: (s: ScreenSettings) => void;
  loaded: boolean;
  loadError: boolean;
  reload: () => void;
} {
  const [settings, setSettings] = useState<ScreenSettings>({});
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const reload = useCallback(() => setReloadKey((k) => k + 1), []);
  useEffect(() => {
    let alive = true;
    const load = () => {
      apiClient.get('/bi/screen-settings', { params: { board } })
        .then((r) => {
          if (alive) { setSettings(r.data ?? {}); setLoadError(false); }
        })
        // 读取失败**保留最后一次已知设置**（不清空）：清空会让空态引导的
        // "添加卡片"以 {cells:{一张卡}} 全量覆盖该看板（审查 P2 #30）
        .catch(() => { if (alive) setLoadError(true); })
        .finally(() => { if (alive) setLoaded(true); });
    };
    load();
    // 投放场景：管理页改设置（模式/主题/筛选）后大屏自动跟随
    const timer = pollMs > 0 ? setInterval(load, pollMs) : undefined;
    return () => {
      alive = false;
      if (timer) clearInterval(timer);
    };
  }, [board, pollMs, reloadKey]);
  const save = (s: ScreenSettings) => {
    if (loadError) {
      message.error('看板设置读取失败，已禁止保存以免覆盖现有布局；请刷新重试');
      return;
    }
    setSettings(s);
    apiClient.put('/bi/screen-settings', s, { params: { board } })
      .catch(() => message.error('看板设置保存失败'));
  };
  return { settings, save, loaded, loadError, reload };
}

export interface BiBoardInfo { key: string; name: string; }

/** 多看板：列表 + 新建/重命名/删除（default 为默认看板，不可删除） */
export function useBoards(): {
  boards: BiBoardInfo[];
  reload: () => void;
  create: (name: string) => Promise<string | null>;
  rename: (key: string, name: string) => Promise<boolean>;
  remove: (key: string) => Promise<boolean>;
} {
  const [boards, setBoards] = useState<BiBoardInfo[]>([]);
  const reload = useCallback(() => {
    apiClient.get('/bi/screens')
      .then((r) => setBoards(r.data ?? []))
      .catch(() => setBoards([]));
  }, []);
  useEffect(reload, [reload]);
  const create = useCallback(async (name: string) => {
    try {
      const r = await apiClient.post('/bi/screens', { name });
      reload();
      return r.data?.key ?? null;
    } catch {
      return null;
    }
  }, [reload]);
  const rename = useCallback(async (key: string, name: string) => {
    try {
      await apiClient.put(`/bi/screens/${key}`, { name });
      reload();
      return true;
    } catch {
      return false;
    }
  }, [reload]);
  const remove = useCallback(async (key: string) => {
    try {
      await apiClient.delete(`/bi/screens/${key}`);
      reload();
      return true;
    } catch {
      return false;
    }
  }, [reload]);
  return { boards, reload, create, rename, remove };
}

/** 按看板布局排图表：order 中的在前（未知/新增的按原顺序追加在后） */
export function applyLayout<T extends { metric_key: string }>(
  defs: T[],
  layout?: ScreenLayout,
): { ordered: T[]; spanOf: (key: string) => number } {
  const byKey = new Map(defs.map((d) => [d.metric_key, d]));
  const ordered: T[] = [];
  for (const k of layout?.order ?? []) {
    const d = byKey.get(k);
    if (d) {
      ordered.push(d);
      byKey.delete(k);
    }
  }
  for (const d of defs) {
    if (byKey.has(d.metric_key)) {
      ordered.push(d);
      byKey.delete(d.metric_key);
    }
  }
  const spanOf = (key: string) => (layout?.spans?.[key] === 2 ? 2 : 1);
  return { ordered, spanOf };
}

export const MetricContent: React.FC<{
  def: BiMetricDef;
  payload?: MetricPayload;
  loading?: boolean;
  dark?: boolean;
  height?: number | string;
  /** 明细表投放态自动轮播滚动（管理页不传） */
  autoScroll?: boolean;
}> = ({ def, payload, loading = false, dark = false, height = 260, autoScroll = false }) => {
  const data = payload?.data;
  const styleCfg = (def.style ?? {}) as Record<string, any>;
  const measureLabels = useMemo(
    () => Object.fromEntries(
      (def.measures ?? []).map((m: any) => [m.key, m.label || m.key]),
    ),
    [def.measures],
  );

  if (def.chart_type === 'detail') {
    return <DetailTable def={def} payload={payload} loading={loading} dark={dark}
                        height={height} autoScroll={autoScroll} />;
  }

  if (!data) {
    return (
      <div style={{ height, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        {loading ? <Spin /> : <Empty description="暂无数据" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
      </div>
    );
  }

  if (def.chart_type === 'number') {
    const values = data.values ?? {};
    const keys = Object.keys(values);
    if (keys.length === 0) {
      return (
        <DataCard label={def.name} value={data.value} unit={data.unit} dark={dark}
                  bgColor={styleCfg.bg_color} fontColor={styleCfg.font_color} />
      );
    }
    return (
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
        {keys.map((k) => {
          const isDuration = k.includes('duration');
          const tone: Tone = k.includes('failed')
            ? 'danger'
            : (k.includes('success') ? 'success' : 'default');
          return (
            <DataCard
              key={k}
              label={measureLabels[k] ?? k}
              value={values[k] === null ? null : Math.round(values[k] * 100) / 100}
              unit={isDuration ? 'ms' : (styleCfg.unit ?? '')}
              tone={tone}
              dark={dark}
              bgColor={styleCfg.bg_color}
              fontColor={styleCfg.font_color}
            />
          );
        })}
      </div>
    );
  }

  if (def.chart_type === 'table') {
    if (dark) return <DarkTable columns={data.columns ?? []} rows={data.rows ?? []} />;
    return (
      <Table
        size="small"
        rowKey="__key"
        columns={(data.columns ?? []).map((c: any) => ({
          title: c.title, dataIndex: c.dataIndex, ellipsis: true,
        }))}
        dataSource={(data.rows ?? []).map((r: any, i: number) => ({ ...r, __key: i }))}
        pagination={{ pageSize: 8, size: 'small', hideOnSinglePage: true }}
        scroll={{ x: 'max-content' }}
      />
    );
  }

  const option = buildOption(def.chart_type, data, dark, styleCfg, styleCfg.unit ?? '');
  return <EChart option={option} height={height} loading={loading} notMerge />;
};
