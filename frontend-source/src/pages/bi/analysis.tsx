import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert, Button, Card, Empty, Input, InputNumber, Modal, Popover, Segmented, Select,
  Space, Spin, Tag, Tooltip, Typography, message,
} from 'antd';
import {
  ArrowDownOutlined, ArrowUpOutlined, CloseOutlined, DeleteOutlined,
  PlusOutlined, SaveOutlined, SearchOutlined,
} from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import apiClient from '../../api/client';
import {
  type BiMetricDef,
  FORMAT_BY_CHART,
  MetricContent,
  nextFreeCell,
  useBoards,
} from './BiBoard';

/**
 * 分析工作台（add-bi-adhoc-analysis）：即席任意维度组合出图
 *
 * 组织对照 DataEase v3 chart editor（dimension/quota 双槽 + 拖拽 + 图表类型面板）：
 * 左字段树 → 拖/点进维度槽或指标槽 → 即时出图（复用看板渲染器）→ 一键另存为看板卡片。
 * 取数走 `POST /bi/adhoc/query`：DB 源自动下推 GROUP BY（不拉全量行），
 * 响应回传引擎与回退原因，本页如实展示（不谎报下推）。
 */

interface ConnOption { id: string; name: string; protocol: string; db_type?: string | null }
interface FormOption { connection_id: string; form_key: string; name?: string | null }
interface FieldMeta {
  path: string; name: string; type: string;
  bi_type: 'dimension' | 'number' | 'time';
}
interface DimItem { name: string; field: string }
interface MeasureItem {
  key: string; agg: string; field: string | null;
  field_type: string; label?: string;
}
interface AdhocResult {
  engine: 'pushdown' | 'memory';
  fallback_reason: string | null;
  row_count: number;
  group_count: number;
  truncated: boolean;
  elapsed_ms: number;
  defn: Record<string, any>;
  format: string;
  data: any;
}

const CHART_OPTIONS = Object.keys(FORMAT_BY_CHART).filter((c) => c !== 'detail');
const AGG_LABELS: Record<string, string> = {
  sum: '求和', avg: '平均', min: '最小', max: '最大',
  count: '计数', count_distinct: '去重计数',
};
const DIM_AGGS = ['count', 'count_distinct'];

/** 数值/时间字段默认算子：数据有量纲时求和，时间是"计数"更常见 */
function defaultAgg(f: FieldMeta): string {
  if (f.bi_type === 'number') return 'sum';
  if (f.bi_type === 'time') return 'count';
  return 'count_distinct';
}

const DIM_ZONE = 'analysis-dimension-zone';
const MEASURE_ZONE = 'analysis-measure-zone';

const BiAnalysisPage: React.FC = () => {
  const navigate = useNavigate();
  const [conns, setConns] = useState<ConnOption[]>([]);
  const [connId, setConnId] = useState<string>();
  const [forms, setForms] = useState<FormOption[]>([]);
  const [formKey, setFormKey] = useState<string>();
  const [fields, setFields] = useState<FieldMeta[]>([]);
  const [fieldsLoading, setFieldsLoading] = useState(false);
  const [keyword, setKeyword] = useState('');

  const [dimensions, setDimensions] = useState<DimItem[]>([]);
  const [measures, setMeasures] = useState<MeasureItem[]>([]);
  const [chartType, setChartType] = useState('bar');
  const [topN, setTopN] = useState(100);
  const [sortBy, setSortBy] = useState<string>();
  const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('desc');

  const [result, setResult] = useState<AdhocResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [saveOpen, setSaveOpen] = useState(false);
  const [cardName, setCardName] = useState('');
  const [cardKey, setCardKey] = useState('');
  const [targetBoard, setTargetBoard] = useState<string>();
  const [saving, setSaving] = useState(false);
  const { boards } = useBoards();

  // ---------- 数据源 ----------
  useEffect(() => {
    apiClient.get('/connections')
      .then((r) => setConns((r.data ?? []).filter(
        (c: ConnOption) => c.db_type || c.protocol === 'hap')))
      .catch(() => setConns([]));
  }, []);

  useEffect(() => {
    setFormKey(undefined);
    setFields([]);
    if (!connId) { setForms([]); return; }
    apiClient.get('/forms', { params: { connection_id: connId } })
      .then((r) => setForms(r.data ?? []))
      .catch(() => setForms([]));
  }, [connId]);

  useEffect(() => {
    setDimensions([]); setMeasures([]); setSortBy(undefined); setResult(null);
    if (!connId || !formKey) { setFields([]); return; }
    setFieldsLoading(true);
    apiClient.get('/bi/form-fields', { params: { connection_id: connId, form_key: formKey } })
      .then((r) => setFields(r.data?.fields ?? []))
      .catch(() => setFields([]))
      .finally(() => setFieldsLoading(false));
  }, [connId, formKey]);

  // ---------- 槽位操作（点击 + 拖拽双通路写同一状态） ----------
  const addDimension = useCallback((f: FieldMeta) => {
    setDimensions((cur) => cur.some((d) => d.field === f.path) || cur.length >= 3
      ? cur
      : [...cur, { name: f.name, field: f.path }]);
  }, []);

  const addMeasure = useCallback((f: FieldMeta) => {
    setMeasures((cur) => {
      if (cur.some((m) => m.field === f.path)) return cur;
      return [...cur, {
        key: f.name, agg: defaultAgg(f), field: f.path,
        field_type: f.bi_type === 'number' ? 'number'
          : f.bi_type === 'time' ? 'point_in_time' : 'string',
      }];
    });
  }, []);

  const addField = useCallback((f: FieldMeta, zone?: string) => {
    if (zone === DIM_ZONE) addDimension(f);
    else if (zone === MEASURE_ZONE) addMeasure(f);
    else if (f.bi_type === 'dimension') addDimension(f);
    else addMeasure(f);
  }, [addDimension, addMeasure]);

  const onDropField = (event: React.DragEvent, zone: string) => {
    event.preventDefault();
    const raw = event.dataTransfer.getData('application/json');
    if (!raw) return;
    try {
      const meta = JSON.parse(raw) as FieldMeta;
      addField(meta, zone);
    } catch { /* 非本页拖拽数据：忽略 */ }
  };

  const allowDrop = (event: React.DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
  };

  // ---------- 即席查询（300ms debounce + 竞态丢弃） ----------
  const seqRef = useRef(0);
  useEffect(() => {
    if (!connId || !formKey || measures.length === 0) {
      setResult(null);
      setError(null);
      return;
    }
    const timer = setTimeout(async () => {
      const seq = ++seqRef.current;
      setLoading(true);
      try {
        const { data } = await apiClient.post('/bi/adhoc/query', {
          connection_id: connId, form_key: formKey, chart_type: chartType,
          dimensions, measures, top_n: topN,
          sort: sortBy ? { by: sortBy, order: sortOrder } : undefined,
          format: FORMAT_BY_CHART[chartType] ?? 'raw',
        });
        if (seq !== seqRef.current) return;      // 过期响应丢弃
        setResult(data as AdhocResult);
        setError(null);
      } catch (e: any) {
        if (seq !== seqRef.current) return;
        const detail = e?.response?.data?.detail;
        const issues = detail?.issues ?? (Array.isArray(detail) ? detail : null);
        setResult(null);
        setError(issues
          ? issues.map((i: any) => i.message).join('；')
          : (typeof detail === 'string' ? detail : '查询失败'));
      } finally {
        if (seq === seqRef.current) setLoading(false);
      }
    }, 300);
    return () => clearTimeout(timer);
  }, [connId, formKey, chartType, dimensions, measures, topN, sortBy, sortOrder]);

  // ---------- 渲染用伪定义（与看板卡同形，渲染器零改动复用） ----------
  const pseudoDef: BiMetricDef = useMemo(() => ({
    metric_key: 'adhoc', name: '即席分析', source_type: 'form',
    source_ref: JSON.stringify({ connection_id: connId, form_key: formKey }),
    chart_type: chartType, dimensions: dimensions as any,
    measures: measures as any, style: {}, top_n: topN,
  } as BiMetricDef), [connId, formKey, chartType, dimensions, measures, topN]);

  const sortOptions = useMemo(() => ([
    ...measures.map((m) => ({ value: m.key, label: `度量：${m.key}` })),
    ...dimensions.map((d) => ({ value: d.name, label: `维度：${d.name}` })),
  ]), [measures, dimensions]);

  const filtered = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    const hit = (f: FieldMeta) => !kw
      || f.name.toLowerCase().includes(kw) || f.path.toLowerCase().includes(kw);
    return {
      dims: fields.filter((f) => f.bi_type === 'dimension' && hit(f)),
      nums: fields.filter((f) => f.bi_type !== 'dimension' && hit(f)),
    };
  }, [fields, keyword]);

  // ---------- 另存为看板卡片 ----------
  const openSave = () => {
    if (!result) return;
    const suffix = Date.now().toString(36).slice(-4);
    setCardName(`${forms.find((f) => f.form_key === formKey)?.name ?? formKey}-分析`);
    setCardKey(`adhoc_${suffix}`);
    setTargetBoard(boards[0]?.key);
    setSaveOpen(true);
  };

  const doSave = async () => {
    if (!result) return;
    setSaving(true);
    try {
      await apiClient.post('/bi/metrics', {
        ...result.defn, metric_key: cardKey, name: cardName, enabled: true,
      });
      let board = targetBoard;
      if (board) {
        const { data: settings } = await apiClient.get('/bi/screen-settings',
          { params: { board } });
        const cells = { ...(settings?.cells ?? {}) };
        if (!cells[cardKey]) {
          const cell = nextFreeCell(cells);
          if (!cell) {
            message.warning('目标看板画布已满，未能摆放卡片；请清理后重试');
            setLoading(false);
            return;
          }
          cells[cardKey] = cell;
        }
        // board 是 query 参数（不是 body）——写进 body 会把设置落到默认看板
        await apiClient.put('/bi/screen-settings', { ...settings, cells },
          { params: { board } });
      }
      message.success('已保存为看板卡片');
      setSaveOpen(false);
      if (board) navigate(`/bi?board=${board}`);
    } catch (e: any) {
      const detail = e?.response?.data?.detail;
      message.error(typeof detail === 'string' ? detail
        : (detail?.message ?? '保存失败（metric_key 可能已存在）'));
    } finally {
      setSaving(false);
    }
  };

  const engineTag = result && (
    result.engine === 'pushdown'
      ? <Tag color="green">聚合下推 · {result.elapsed_ms}ms · 分组 {result.group_count}</Tag>
      : <Tag color="orange">内存聚合 · {result.elapsed_ms}ms · 源行 {result.row_count}
        {result.truncated ? '（已截断）' : ''}</Tag>
  );

  const fieldChip = (f: FieldMeta) => (
    <div
      key={f.path}
      draggable
      data-field={f.path}
      data-bitype={f.bi_type}
      data-testid="analysis-field-chip"
      onDragStart={(e) => {
        e.dataTransfer.setData('application/json', JSON.stringify(f));
        e.dataTransfer.effectAllowed = 'copy';
      }}
      onClick={() => addField(f)}
      title={`${f.name}（${f.type}）`}
      style={{
        padding: '2px 8px', marginBottom: 4, borderRadius: 4, cursor: 'grab',
        background: f.bi_type === 'dimension' ? '#eef4ff' : '#f2fbf3',
        border: '1px solid #e5e7eb', fontSize: 12, display: 'flex',
        justifyContent: 'space-between', gap: 6,
      }}
    >
      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {f.name}
      </span>
      <span style={{ color: '#9ca3af', fontSize: 11 }}>{f.bi_type === 'dimension' ? '维' : '指'}</span>
    </div>
  );

  return (
    <div style={{ padding: 16 }}>
      <Space style={{ marginBottom: 12 }} wrap>
        <Typography.Text strong>分析工作台</Typography.Text>
        <Select
          style={{ width: 200 }} placeholder="选择连接" value={connId}
          onChange={setConnId} showSearch optionFilterProp="label"
          options={conns.map((c) => ({ value: c.id, label: c.name }))}
        />
        <Select
          style={{ width: 220 }} placeholder="选择表单" value={formKey}
          onChange={setFormKey} showSearch optionFilterProp="label"
          options={forms.map((f) => ({
            value: f.form_key, label: f.name ? `${f.name}(${f.form_key})` : f.form_key,
          }))}
        />
        <Select
          style={{ width: 150 }} value={chartType} onChange={setChartType}
          options={CHART_OPTIONS.map((c) => ({ value: c, label: c }))}
        />
        <span style={{ fontSize: 12 }}>
          Top N <InputNumber size="small" min={1} max={1000} value={topN}
                             onChange={(v) => setTopN(v ?? 100)} style={{ width: 80 }} />
        </span>
        <Select
          style={{ width: 170 }} allowClear placeholder="排序" value={sortBy}
          onChange={(v) => setSortBy(v ?? undefined)} options={sortOptions}
        />
        <Segmented
          size="small" value={sortOrder}
          onChange={(v) => setSortOrder(v as 'asc' | 'desc')}
          options={[
            { value: 'desc', icon: <ArrowDownOutlined />, label: '降序' },
            { value: 'asc', icon: <ArrowUpOutlined />, label: '升序' },
          ]}
        />
        <Button icon={<SaveOutlined />} disabled={!result} onClick={openSave}>
          另存为看板卡片
        </Button>
      </Space>

      <div style={{ display: 'flex', gap: 12, alignItems: 'flex-start' }}>
        {/* 字段树 */}
        <Card size="small" title="字段" style={{ width: 240, flex: '0 0 auto' }}
              styles={{ body: { maxHeight: 560, overflow: 'auto' } }}>
          <Input size="small" allowClear prefix={<SearchOutlined />} placeholder="搜索字段"
                 value={keyword} onChange={(e) => setKeyword(e.target.value)}
                 style={{ marginBottom: 8 }} />
          {fieldsLoading ? <Spin size="small" /> : (
            <>
              <div style={{ fontSize: 12, color: '#6b7280', margin: '4px 0' }}>
                维度（拖入或点击）
              </div>
              {filtered.dims.map(fieldChip)}
              {filtered.dims.length === 0 && <div style={{ fontSize: 12, color: '#9ca3af' }}>无</div>}
              <div style={{ fontSize: 12, color: '#6b7280', margin: '8px 0 4px' }}>
                指标（数值/时间）
              </div>
              {filtered.nums.map(fieldChip)}
              {filtered.nums.length === 0 && <div style={{ fontSize: 12, color: '#9ca3af' }}>无</div>}
            </>
          )}
        </Card>

        {/* 槽位 + 画布 */}
        <div style={{ flex: 1, minWidth: 0 }}>
          <Card size="small" style={{ marginBottom: 12 }}
                title="维度与指标（拖到这里，或点左侧字段）">
            <Zone
              testId={DIM_ZONE}
              label="维度"
              empty="拖入维度字段（最多 3 个）"
              onDrop={(e) => onDropField(e, DIM_ZONE)}
              onDragOver={allowDrop}
            >
              {dimensions.map((d) => (
                <Tag key={d.field} data-field={d.field} closable
                     closeIcon={<CloseOutlined />}
                     onClose={() => {
                       setDimensions((cur) => cur.filter((x) => x.field !== d.field));
                       setSortBy((cur) => (cur === d.name ? undefined : cur));
                     }}>{d.name}</Tag>
              ))}
            </Zone>
            <Zone
              testId={MEASURE_ZONE}
              label="指标"
              empty="拖入数值/时间字段（计数可直接用文本字段）"
              onDrop={(e) => onDropField(e, MEASURE_ZONE)}
              onDragOver={allowDrop}
            >
              {measures.map((m) => (
                <Tag key={m.field ?? m.key} data-field={m.field ?? m.key}
                     style={{ paddingInlineEnd: 2 }}>
                  <Popover
                    trigger="click"
                    content={(
                      <Space direction="vertical" size={4}>
                        {(m.field_type === 'number'
                          ? ['sum', 'avg', 'min', 'max', 'count', 'count_distinct']
                          : DIM_AGGS).map((agg) => (
                          <Button key={agg} size="small" type={
                            m.agg === agg ? 'primary' : 'default'}
                            onClick={() => setMeasures((cur) => cur.map((x) => (
                              (x.field ?? x.key) === (m.field ?? m.key)
                                ? { ...x, agg } : x)))}>
                            {AGG_LABELS[agg]}
                          </Button>
                        ))}
                      </Space>
                    )}
                  >
                    <span style={{ cursor: 'pointer' }}>
                      {m.key}·{AGG_LABELS[m.agg] ?? m.agg}
                    </span>
                  </Popover>
                  <Button type="text" size="small" icon={<DeleteOutlined />}
                          onClick={() => {
                            setMeasures((cur) => cur.filter(
                              (x) => (x.field ?? x.key) !== (m.field ?? m.key)));
                            setSortBy((cur) => (cur === m.key ? undefined : cur));
                          }} />
                </Tag>
              ))}
            </Zone>
            <div style={{ marginTop: 6, fontSize: 12, color: '#9ca3af' }}>
              <PlusOutlined /> 提示：DB 源自动下推源库 GROUP BY（不拉全量行）；
              含计算字段/分位算子会回退内存路径，标签会如实标注。
            </div>
          </Card>

          {error && (
            <Alert type="warning" showIcon style={{ marginBottom: 12 }}
                   message="无法出图" description={error} />
          )}

          <Card size="small" title={<Space>{engineTag}
            {result?.format && <span style={{ fontSize: 12, color: '#9ca3af' }}>
              格式 {result.format}
            </span>}</Space>}>
            {measures.length === 0 ? (
              <Empty description="先选数据源并添加至少一个指标"
                     image={Empty.PRESENTED_IMAGE_SIMPLE} style={{ padding: 40 }} />
            ) : (
              <MetricContent def={pseudoDef} loading={loading} height={380}
                             payload={result
                               ? { metricKey: 'adhoc', chartType, data: result.data }
                               : undefined} />
            )}
          </Card>
        </div>
      </div>

      <Modal
        open={saveOpen} title="另存为看板卡片" onCancel={() => setSaveOpen(false)}
        onOk={doSave} confirmLoading={saving} okText="保存" width={460}
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <div>
            <div style={{ fontSize: 12, color: '#6b7280', marginBottom: 4 }}>卡片名称</div>
            <Input value={cardName} onChange={(e) => setCardName(e.target.value)} />
          </div>
          <div>
            <div style={{ fontSize: 12, color: '#6b7280', marginBottom: 4 }}>
              指标标识（metric_key，英文/数字/下划线，需唯一）
            </div>
            <Input value={cardKey} onChange={(e) => setCardKey(e.target.value)} />
          </div>
          <div>
            <div style={{ fontSize: 12, color: '#6b7280', marginBottom: 4 }}>
              摆放到看板（可选，自动找空位）
            </div>
            <Select
              style={{ width: '100%' }} allowClear value={targetBoard}
              onChange={(v) => setTargetBoard(v ?? undefined)}
              options={boards.map((b) => ({ value: b.key, label: b.name }))}
            />
          </div>
          <Tooltip title="保存后可在看板编辑模式调整位置与尺寸">
            <span style={{ fontSize: 12, color: '#9ca3af' }}>
              定义与看板卡片同源：保存后数值与当前工作台一致
            </span>
          </Tooltip>
        </Space>
      </Modal>
    </div>
  );
};

/** 拖拽落点区（原生 HTML5 DnD；data-testid 供 UI 自审脚本定位） */
const Zone: React.FC<{
  testId: string; label: string; empty: string;
  onDrop: (e: React.DragEvent) => void;
  onDragOver: (e: React.DragEvent) => void;
  children: React.ReactNode;
}> = ({ testId, label, empty, onDrop, onDragOver, children }) => {
  const [hover, setHover] = useState(false);
  const hasChild = React.Children.count(children) > 0;
  return (
    <div
      data-testid={testId}
      onDrop={(e) => { setHover(false); onDrop(e); }}
      onDragOver={(e) => { setHover(true); onDragOver(e); }}
      onDragLeave={() => setHover(false)}
      style={{
        display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap',
        minHeight: 40, padding: '4px 8px', marginBottom: 8, borderRadius: 6,
        border: `1px dashed ${hover ? '#2383E2' : '#d9d9d9'}`,
        background: hover ? '#f0f7ff' : '#fafafa',
      }}
    >
      <span style={{ fontSize: 12, color: '#6b7280', flex: '0 0 auto' }}>{label}</span>
      {hasChild ? children
        : <span style={{ fontSize: 12, color: '#bfbfbf' }}>{empty}</span>}
    </div>
  );
};

export default BiAnalysisPage;
