import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert, AutoComplete, Button, Card, Checkbox, ColorPicker, Form, Input, InputNumber,
  Modal, Popconfirm, Select, Space, Switch, Table, Tag, Tooltip, message,
} from 'antd';
import { DeleteOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import apiClient from '../../api/client';
import { parseFormRef, type BiMetricDef } from './BiBoard';

/** 与后端 bi_defs.SUPPORTED_AGGS 保持一致 */
const AGGS = ['count', 'sum', 'avg', 'min', 'max', 'p50', 'p95', 'p99',
  'count_distinct', 'ratio'];
const NUMERIC_AGGS = new Set(['sum', 'avg', 'min', 'max', 'p50', 'p95', 'p99']);
const AGG_LABELS: Record<string, string> = {
  count: 'count（计数）', sum: 'sum（求和）', avg: 'avg（平均）',
  min: 'min（最小）', max: 'max（最大）', p50: 'p50（中位数）',
  p95: 'p95', p99: 'p99', count_distinct: 'count_distinct（去重计数）',
  ratio: 'ratio（比率：分子÷分母）',
};
/** 计算字段可用函数（与后端 bi_expr 白名单一致） */
const EXPR_FUNCS = [
  'round(x[,d])', 'abs(x)', 'ceil(x)', 'floor(x)', 'min(..)', 'max(..)',
  'coalesce(..)', 'substring(s,start[,len])', 'upper(s)', 'lower(s)', 'trim(s)',
  'concat(..)', "date_part(s,'year'|'month'|'day')",
];
const FIELD_TYPES = [
  { value: 'number', label: '数值' },
  { value: 'duration', label: '时长（可求和/平均）' },
  { value: 'point_in_time', label: '时间点（仅可 min/max）' },
  { value: 'string', label: '文本' },
];
const SOURCE_TYPES = [
  { value: 'form', label: '连接表单（业务数据）' },
  { value: 'data_buffer', label: '中间数据 data_buffer' },
  { value: 'execution_logs', label: '执行日志 execution_logs' },
  { value: 'custom_sql', label: '自定义只读 SQL' },
];
const GRANULARITIES = ['1min', '5min', '1h', '1d'].map((v) => ({ value: v, label: v }));
const CHART_TYPES = [
  { value: 'bar', label: '柱状' },
  { value: 'line', label: '折线' },
  { value: 'time_line', label: '折线（时间坐标系）' },
  { value: 'pie', label: '饼图' },
  { value: 'scatter', label: '散点图' },
  { value: 'radar', label: '雷达图' },
  { value: 'heatmap', label: '热力图（矩阵 2D）' },
  { value: 'bar3d', label: '3D 矩阵' },
  { value: 'sankey', label: '桑基图' },
  { value: 'graph', label: '关系图' },
  { value: 'flow', label: '途径图（有向）' },
  { value: 'tree', label: '树图' },
  { value: 'funnel', label: '漏斗图' },
  { value: 'gauge', label: '仪表盘' },
  { value: 'table', label: '表格' },
  { value: 'number', label: '指标卡' },
  { value: 'detail', label: '明细表（表单原始行）' },
];
/** 关系族图表：需要 ≥2 个分组维度 */
const RELATION_CHARTS = new Set(['sankey', 'graph', 'flow', 'tree']);

/** 筛选操作符（与后端 bi_defs.FILTER_OPS_BY_TYPE 保持一致） */
const FILTER_OP_LABELS: Record<string, string> = {
  eq: '等于', ne: '不等于',
  gt: '大于', ge: '大于等于', lt: '小于', le: '小于等于',
  in: '属于', not_in: '不属于',
  contains: '包含', not_contains: '不包含',
  between: '区间（最小~最大）', not_between: '区间之外',
  empty: '为空', not_empty: '不为空',
};
const FILTER_OPS_BY_TYPE: Record<string, string[]> = {
  dimension: ['eq', 'ne', 'in', 'not_in', 'contains', 'not_contains', 'empty', 'not_empty'],
  number: ['eq', 'ne', 'gt', 'ge', 'lt', 'le', 'between', 'not_between', 'empty', 'not_empty'],
  time: ['between', 'gt', 'lt', 'eq', 'ne', 'empty', 'not_empty'],
};
const ALL_FILTER_OPS = Array.from(new Set(Object.values(FILTER_OPS_BY_TYPE).flat()));
const opsForFieldType = (ft?: string): Array<{ value: string; label: string }> => {
  const ops = ft && FILTER_OPS_BY_TYPE[ft] ? FILTER_OPS_BY_TYPE[ft] : ALL_FILTER_OPS;
  return ops.map((o) => ({ value: o, label: FILTER_OP_LABELS[o] ?? o }));
};

interface DimCandidate {
  name: string;
  code: string;
  cardinality: 'low' | 'medium' | 'high';
  usable_for_group: boolean;
  hint: string;
}

/** /bi/form-fields 返回的字段元数据（含 BI 逻辑类型，借鉴 DataEase deType+groupType） */
interface FormFieldMeta {
  path: string;
  name: string;
  type: string;
  bi_type: 'dimension' | 'number' | 'time';
}

interface ConnOption {
  id: string;
  name: string;
  db_type: string | null;
  protocol: string | null;
}

interface FormOption {
  form_key: string;
  display_name: string | null;
  connection_id: string;
}

const BI_TYPE_LABEL: Record<string, string> = {
  dimension: '维度', number: '数值', time: '时间',
};

/** BI 逻辑类型 → 度量 field_type（与后端校验规则对齐） */
const biTypeToFieldType = (bi?: string): string => (
  bi === 'number' ? 'number' : bi === 'time' ? 'point_in_time' : 'string'
);

const CARD_TONE: Record<string, string> = {
  low: 'green', medium: 'orange', high: 'red',
};
const CARD_LABEL: Record<string, string> = {
  low: '低基数', medium: '中基数', high: '高基数',
};

/** 指标定义配置页（2026-09-25 重定位：表单源为主入口） */
const BiDefinitionsPage: React.FC = () => {
  const [defs, setDefs] = useState<BiMetricDef[]>([]);
  const [candidates, setCandidates] = useState<DimCandidate[]>([]);
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState<BiMetricDef | null>(null);
  const [open, setOpen] = useState(false);
  const [issues, setIssues] = useState<any[]>([]);
  const [estimate, setEstimate] = useState<any>(null);
  const [conns, setConns] = useState<ConnOption[]>([]);
  const [forms, setForms] = useState<FormOption[]>([]);
  const [fieldMetas, setFieldMetas] = useState<FormFieldMeta[]>([]);
  const [fieldsLoading, setFieldsLoading] = useState(false);
  // 维度分组编辑器（值 → 组映射；add-bi-calc-fields）
  const [groupEdit, setGroupEdit] = useState<{
    idx: number;
    rows: { name: string; values: string }[];
    other: string;
  } | null>(null);
  const [form] = Form.useForm();
  const sourceType = Form.useWatch('source_type', form);
  const connId = Form.useWatch('connection_id', form);
  const formKey = Form.useWatch('form_key', form);
  const chartType = Form.useWatch('chart_type', form);
  const isForm = sourceType === 'form';
  const isDetail = chartType === 'detail';

  const loadDefs = useCallback(async () => {
    setLoading(true);
    try {
      const res = await apiClient.get('/bi/metrics');
      setDefs(res.data ?? []);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadDefs();
    apiClient.get('/bi/dimensions')
      .then((res) => setCandidates(res.data ?? []))
      .catch(() => setCandidates([]));
    apiClient.get('/connections')
      .then((res) => setConns(
        (res.data ?? []).filter((c: ConnOption) => c.db_type || c.protocol === 'hap'),
      ))
      .catch(() => setConns([]));
  }, [loadDefs]);

  // 表单注册表按连接过滤（表单注册表为下拉主来源，手输兜底）
  useEffect(() => {
    if (!isForm || !connId) {
      setForms([]);
      return;
    }
    apiClient.get('/forms')
      .then((res) => setForms(
        ((res.data ?? []) as FormOption[]).filter((f) => f.connection_id === connId),
      ))
      .catch(() => setForms([]));
  }, [isForm, connId]);

  // 字段元数据（维度/度量候选 + 明细列标题）
  useEffect(() => {
    if (!isForm || !connId || !formKey) {
      setFieldMetas([]);
      return;
    }
    setFieldsLoading(true);
    apiClient.get('/bi/form-fields', { params: { connection_id: connId, form_key: formKey } })
      .then((res) => setFieldMetas(res.data?.fields ?? []))
      .catch(() => setFieldMetas([]))
      .finally(() => setFieldsLoading(false));
  }, [isForm, connId, formKey]);

  const fieldOptions = fieldMetas.map((f) => ({
    value: f.path,
    label: `${f.name}（${BI_TYPE_LABEL[f.bi_type] ?? f.type}）`,
  }));

  const candidateOf = (name: string): DimCandidate | undefined => {
    const v = String(name ?? '').trim();
    return candidates.find((c) => c.name === v || c.code === v);
  };

  const openCreate = () => {
    setEditing(null);
    setIssues([]);
    setEstimate(null);
    form.resetFields();
    form.setFieldsValue({
      source_type: 'form', granularity: '1h', chart_type: 'bar',
      top_n: 100, enabled: false, time_field: 'created_at',
      dimensions: [], measures: [{ key: 'count', agg: 'count', field_type: 'number' }],
      filters: [], computed: [], display: {},
    });
    setOpen(true);
  };

  const openEdit = (def: BiMetricDef) => {
    setEditing(def);
    setIssues([]);
    setEstimate(null);
    // 先清残留再回填：filters/style 等条件回填字段会把上一条定义的值带进来
    form.resetFields();
    form.setFieldsValue({
      metric_key: def.metric_key,
      name: def.name,
      source_type: def.source_type,
      granularity: def.granularity,
      chart_type: def.chart_type,
      top_n: def.top_n,
      enabled: def.enabled,
      time_field: def.time_field || 'created_at',
      dimensions: (def.dimensions ?? []).map((d: any) => ({
        name: d.name, field: d.field, resolver: d.resolver,
        groups: d.groups, other: d.other,
      })),
      measures: (def.measures ?? []).map((m: any) => ({
        key: m.key, agg: m.agg, field: m.field,
        field_type: m.field_type ?? 'number',
        numerator: m.numerator, denominator: m.denominator, scale: m.scale,
      })),
      computed: (def as any).computed ?? [],
      display: (def as any).display ?? {},
    });
    if (def.source_type === 'form') {
      const ref = parseFormRef(def.source_ref);
      form.setFieldsValue({ connection_id: ref.connectionId || undefined, form_key: ref.formKey });
    }
    // 旧等值形态 {字段: 值} 转为可编辑规则；结构化 list 原样回填
    const legacyRules = def.filters && !Array.isArray(def.filters)
      ? Object.entries(def.filters).map(([k, v]) => ({ field: k, op: 'eq', value: v }))
      : null;
    if (legacyRules || Array.isArray(def.filters)) {
      form.setFieldsValue({
        filters: (legacyRules ?? (def.filters as any[])).map((r) => ({ ...r })),
      });
    }
    if (def.style) {
      form.setFieldsValue({ style: def.style });
    }
    setOpen(true);
  };

  /** 前端前置校验：与后端规则一致，先给即时反馈 */
  const precheck = (values: any): string | null => {
    if (values.chart_type === 'detail') {
      return null; // 明细表无维度/度量
    }
    const nMeasures = (values.measures ?? []).filter((m: any) => m?.key && m?.agg).length;
    const nDims = (values.dimensions ?? []).filter((d: any) => d?.name && (d?.field || d?.resolver)).length;
    if (values.chart_type === 'scatter' && nMeasures !== 2) {
      return '散点图需要恰好 2 个度量（作为 x/y 轴）';
    }
    if (RELATION_CHARTS.has(values.chart_type) && nDims < 2) {
      return '桑基/关系/途径/树图需要至少 2 个分组维度（构成层级/流向关系）';
    }
    for (const m of values.measures ?? []) {
      if (!m) continue;
      if (m.agg === 'ratio') {
        if (!m.numerator?.field || !m.denominator?.field) {
          return `度量「${m.key || 'ratio'}」使用 ratio 时必须配置分子与分母字段`;
        }
        continue;
      }
      if (NUMERIC_AGGS.has(m.agg) && !m.field) {
        return `度量「${m.key || m.agg}」使用 ${m.agg} 时必须填写字段`;
      }
      if (m.field_type === 'point_in_time' && (m.agg === 'sum' || m.agg === 'avg')) {
        return `时间点字段不允许 ${m.agg}（结果无意义），仅允许 min/max`;
      }
    }
    for (const c of values.computed ?? []) {
      if (!c) continue;
      if (!String(c.key || '').trim() || !String(c.expr || '').trim()) {
        return '计算字段需要填写标识与表达式';
      }
    }
    if (values.display?.cumulative && values.source_type === 'form') {
      return '快照源（连接表单）不支持累计修饰（每轮都是当前状态）';
    }
    if ((values.dimensions ?? []).length > 3) {
      return '分组维度最多 3 个（时间维不计入）';
    }
    return null;
  };

  const submit = async () => {
    const values = await form.validateFields();
    const pre = precheck(values);
    if (pre) {
      setIssues([{ level: 'error', code: 'precheck', message: pre }]);
      return;
    }
    const payload: any = { ...values };
    delete payload.connection_id;
    delete payload.form_key;
    payload.dimensions = (values.dimensions ?? [])
      .filter((d: any) => d?.name && (d?.field || d?.resolver));
    payload.measures = (values.measures ?? [])
      .filter((m: any) => m?.key && m?.agg);
    // 计算字段（行级派生列）：只保留完整的项
    payload.computed = (values.computed ?? [])
      .filter((c: any) => c?.key && c?.expr)
      .map((c: any) => ({
        key: c.key, name: c.name || c.key, expr: c.expr,
        field_type: c.field_type || 'number',
      }));
    // 展示修饰：剔除未开启项（空对象不落库）
    const disp: Record<string, boolean> = {};
    if (values.display?.percent) disp.percent = true;
    if (values.display?.cumulative) disp.cumulative = true;
    payload.display = disp;
    payload.filters = (values.filters ?? [])
      .filter((r: any) => r?.field && r?.op)
      .map((r: any) => ({
        ...r,
        // 区间/属于：保留数组；空值操作符不带值
        value: (r.op === 'empty' || r.op === 'not_empty') ? undefined : r.value,
      }));
    if (values.source_type === 'form') {
      payload.source_ref = JSON.stringify({
        connection_id: values.connection_id, form_key: values.form_key,
      });
    }
    try {
      if (editing) {
        await apiClient.put(`/bi/metrics/${editing.metric_key}`, payload);
      } else {
        await apiClient.post('/bi/metrics', payload);
      }
      message.success(editing ? '已更新' : '已创建');
      setOpen(false);
      await loadDefs();
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      if (detail?.issues) setIssues(detail.issues);
      else setIssues([{ level: 'error', code: 'error', message: String(detail ?? err?.message) }]);
    }
  };

  const removeDef = async (key: string) => {
    await apiClient.delete(`/bi/metrics/${key}`);
    message.success('已删除');
    await loadDefs();
  };

  const runEstimate = async () => {
    if (!editing) {
      message.info('基数预估需要先将指标保存后再执行');
      return;
    }
    try {
      const res = await apiClient.post(`/bi/metrics/${editing.metric_key}/estimate`);
      setEstimate(res.data);
    } catch {
      /* 拦截器已提示 */
    }
  };

  const dimColumns = [
    { title: '维度名', dataIndex: 'name' },
    {
      title: '类型', dataIndex: 'cardinality',
      render: (v: string) => v
        ? <Tag color={CARD_TONE[v]}>{CARD_LABEL[v] ?? v}</Tag>
        : <span style={{ color: 'var(--nb-text-muted)' }}>自定义</span>,
    },
    { title: '用法提示', dataIndex: 'hint' },
  ];

  return (
    <div>
      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        marginBottom: 20, flexWrap: 'wrap', gap: 12,
      }}>
        <h1 className="page-heading" style={{ marginBottom: 0 }}>BI 指标定义</h1>
        <Space>
          <Button size="small" icon={<ReloadOutlined />} onClick={loadDefs}>刷新</Button>
          <Button size="small" type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            新建指标
          </Button>
        </Space>
      </div>

      <Card size="small" title="候选业务维度（保存前请确认基数风险）" style={{ marginBottom: 16 }}>
        <Table
          size="small" rowKey="code" pagination={false}
          columns={dimColumns} dataSource={candidates}
        />
        <div style={{ marginTop: 8, fontSize: 12, color: 'var(--nb-text-secondary)' }}>
          表单数据源会按字段自动标注「维度 / 数值 / 时间」；物料 / 计划号等高基数字段
          仅建议用于 Top N 排行而非分组。
        </div>
      </Card>

      <Card size="small" title="指标列表">
        <Table
          size="small" rowKey="metric_key" loading={loading}
          dataSource={defs}
          pagination={{ pageSize: 12, hideOnSinglePage: true }}
          columns={[
            { title: '标识', dataIndex: 'metric_key', width: 200 },
            { title: '名称', dataIndex: 'name' },
            { title: '数据源', dataIndex: 'source_type', width: 150 },
            { title: '粒度', dataIndex: 'granularity', width: 80 },
            { title: '图表', dataIndex: 'chart_type', width: 90 },
            {
              title: '维度', dataIndex: 'dimensions', width: 100,
              render: (d: any[]) => d?.length ?? 0,
            },
            {
              title: '状态', dataIndex: 'enabled', width: 90,
              render: (v: boolean, r: BiMetricDef) => (
                <Switch size="small" checked={v} onChange={async (checked) => {
                  await apiClient.put(`/bi/metrics/${r.metric_key}`, {
                    ...r, enabled: checked,
                  });
                  await loadDefs();
                }} />
              ),
            },
            {
              title: '操作', width: 140,
              render: (_: unknown, r: BiMetricDef) => (
                <Space size={4}>
                  <Button size="small" type="link" onClick={() => openEdit(r)}>编辑</Button>
                  <Popconfirm title="删除该指标及其聚合结果？" onConfirm={() => removeDef(r.metric_key)}>
                    <Button size="small" type="link" danger>删除</Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Modal
        open={open} title={editing ? `编辑指标：${editing.metric_key}` : '新建指标'}
        width={880} onCancel={() => setOpen(false)} onOk={submit}
        okText="保存" destroyOnHidden
      >
        {issues.length > 0 ? (
          <Alert
            type="error" style={{ marginBottom: 12 }} showIcon
            message="校验未通过"
            description={(
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {issues.map((i, idx) => (
                  <li key={idx}>
                    {i.message}
                    {i.code === 'high_cardinality_dimension' ? '（提示，不阻断）' : ''}
                  </li>
                ))}
              </ul>
            )}
          />
        ) : null}
        {estimate ? (
          <Alert
            type={estimate.threshold_exceeded ? 'warning' : 'info'}
            style={{ marginBottom: 12 }} showIcon
            message={`最近窗口维度组合数：${estimate.combinations}`
              + (estimate.threshold_exceeded ? '（超过阈值，建议减少维度或依赖 Top N 截断）' : '')}
          />
        ) : null}

        <Form form={form} layout="vertical" size="small">
          <Space style={{ display: 'flex' }} align="start" wrap>
            <Form.Item name="metric_key" label="指标标识" rules={[{ required: true }]}
                       style={{ width: 200 }}>
              <Input placeholder="如 product_catalog_summary" disabled={Boolean(editing)} />
            </Form.Item>
            <Form.Item name="name" label="显示名称" rules={[{ required: true }]}
                       style={{ width: 200 }}>
              <Input placeholder="如 产品目录汇总" />
            </Form.Item>
            <Form.Item name="source_type" label="数据源" style={{ width: 190 }}>
              <Select options={SOURCE_TYPES} />
            </Form.Item>
            <Form.Item name="granularity" label="时间粒度" style={{ width: 100 }}>
              <Select options={GRANULARITIES} />
            </Form.Item>
            <Form.Item name="chart_type" label="图表类型" style={{ width: 130 }}>
              <Select options={CHART_TYPES} />
            </Form.Item>
            <Form.Item name="top_n" label="Top N" style={{ width: 90 }}>
              <InputNumber min={0} max={1000} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="enabled" label="启用" valuePropName="checked">
              <Switch />
            </Form.Item>
          </Space>

          {chartType === 'number' && (
            <Space style={{ display: 'flex' }} align="start" wrap>
              <Form.Item name={['style', 'bg_color']} label="KPI 底色"
                         getValueFromEvent={(c: any) => c?.toHexString?.() ?? c}>
                <ColorPicker showText allowClear />
              </Form.Item>
              <Form.Item name={['style', 'font_color']} label="字体颜色"
                         getValueFromEvent={(c: any) => c?.toHexString?.() ?? c}>
                <ColorPicker showText allowClear />
              </Form.Item>
              <div style={{ fontSize: 12, color: 'var(--nb-text-secondary)', paddingTop: 30 }}>
                不设置时：大屏投放用深色卡，管理页用白底卡
              </div>
            </Space>
          )}
          {chartType === 'gauge' && (
            <Space style={{ display: 'flex' }} align="start" wrap>
              <Form.Item name={['style', 'gauge_min']} label="量程下限">
                <InputNumber style={{ width: 140 }} />
              </Form.Item>
              <Form.Item name={['style', 'gauge_max']} label="量程上限">
                <InputNumber style={{ width: 140 }} placeholder="默认 100" />
              </Form.Item>
            </Space>
          )}
          {chartType === 'scatter' && (
            <Alert type="info" showIcon style={{ marginBottom: 12 }}
                   message="散点图：第 1 个度量为 x 轴、第 2 个度量为 y 轴，每个快照桶产生一组数据点" />
          )}
          {RELATION_CHARTS.has(chartType) && (
            <Alert type="info" showIcon style={{ marginBottom: 12 }}
                   message="桑基/关系/途径/树图：按「维度 1 → 维度 2（→ 维度 3）」的层级或流向组织，需至少 2 个维度；途径图以有向箭头表达流向" />
          )}

          {isForm && (
            <>
              <Space style={{ display: 'flex' }} align="start" wrap>
                <Form.Item name="connection_id" label="连接" rules={[{ required: true }]}
                           style={{ width: 220 }}>
                  <Select
                    showSearch placeholder="选择 HAP / 数据库连接"
                    options={conns.map((c) => ({ value: c.id, label: c.name }))}
                    optionFilterProp="label"
                  />
                </Form.Item>
                <Form.Item name="form_key" label="表单" rules={[{ required: true }]}
                           style={{ width: 280 }}>
                  <AutoComplete
                    showSearch placeholder="注册表选择，或手输 worksheet_id / 表名"
                    options={forms.map((f) => ({
                      value: f.form_key,
                      label: `${f.display_name || f.form_key}`,
                    }))}
                    filterOption={(input, option) =>
                      String(option?.label ?? '').toLowerCase()
                        .includes(input.toLowerCase())}
                  />
                </Form.Item>
              </Space>
              <Alert
                type="info" showIcon style={{ marginBottom: 12 }}
                message={fieldsLoading
                  ? '字段加载中…'
                  : (fieldMetas.length > 0
                    ? `已加载 ${fieldMetas.length} 个字段（维度 / 数值 / 时间由后端自动归类）`
                    : '选择连接与表单后自动加载字段，作为维度与度量候选')}
              />
            </>
          )}

          {!isDetail && (
            <>
              <Form.Item label="分组维度（最多 3 个，时间维不计入）" style={{ marginBottom: 0 }}>
                <Form.List name="dimensions">
                  {(fields, { add, remove }) => (
                    <>
                      {fields.map((field) => (
                        <Space key={field.key} align="baseline" style={{ display: 'flex' }}>
                          <Form.Item name={[field.name, 'name']}
                                     style={{ marginBottom: 8 }}>
                            {isForm ? (
                              <Input placeholder="随字段自动填中文名" style={{ width: 160 }} />
                            ) : (
                              <Select
                                showSearch allowClear placeholder="维度名（候选可提示基数）"
                                style={{ width: 200 }}
                                options={candidates.map((c) => ({
                                  value: c.name,
                                  label: `${c.name}（${CARD_LABEL[c.cardinality]}）`,
                                }))}
                              />
                            )}
                          </Form.Item>
                          <Form.Item name={[field.name, 'field']}
                                     style={{ marginBottom: 8 }}>
                            {isForm ? (
                              <Select
                                showSearch allowClear placeholder="选择维度字段"
                                style={{ width: 250 }}
                                options={fieldOptions}
                                onChange={(v) => {
                                  const meta = fieldMetas.find((f) => f.path === v);
                                  if (meta) {
                                    form.setFieldValue(
                                      ['dimensions', field.name, 'name'], meta.name);
                                  }
                                }}
                              />
                            ) : (
                              <Input placeholder="JSON 路径 / 列名，如 category"
                                     style={{ width: 240 }} />
                            )}
                          </Form.Item>
                          <Form.Item shouldUpdate noStyle>
                            {() => {
                              const d = form.getFieldValue(['dimensions', field.name]);
                              const hit = candidateOf(d?.name);
                              if (!hit || hit.cardinality !== 'high') return null;
                              return (
                                <Tooltip title={hit.hint}><Tag color="red">高基数</Tag></Tooltip>
                              );
                            }}
                          </Form.Item>
                          <Button size="small" onClick={() => {
                            const d = form.getFieldValue(['dimensions', field.name]) ?? {};
                            setGroupEdit({
                              idx: field.name,
                              rows: (d.groups ?? []).map((g: any) => ({
                                name: g.name ?? '',
                                values: (g.values ?? []).join(', '),
                              })),
                              other: d.other ?? '',
                            });
                          }}>
                            分组{(form.getFieldValue(['dimensions', field.name])?.groups
                              ?.length ? ' ✓' : '')}
                          </Button>
                          <Button size="small" type="text" danger icon={<DeleteOutlined />}
                                  onClick={() => remove(field.name)} />
                        </Space>
                      ))}
                      <Button size="small" type="dashed" icon={<PlusOutlined />}
                              onClick={() => add({ name: '', field: undefined })}>
                        添加维度
                      </Button>
                    </>
                  )}
                </Form.List>
              </Form.Item>

              <Form.Item label="度量（算子 + 字段；ratio=分子÷分母的整体比率）"
                         style={{ marginBottom: 0 }}>
                <Form.List name="measures">
                  {(fields, { add, remove }) => (
                    <>
                      {fields.map((field) => (
                        <Space key={field.key} align="baseline" style={{ display: 'flex' }} wrap>
                          <Form.Item name={[field.name, 'key']}
                                     rules={[{ required: true, message: '填写度量名' }]}
                                     style={{ marginBottom: 8 }}>
                            <Input placeholder="度量标识，如 amount_sum" style={{ width: 180 }} />
                          </Form.Item>
                          <Form.Item name={[field.name, 'agg']}
                                     style={{ marginBottom: 8 }}>
                            <Select style={{ width: 190 }}
                                    options={AGGS.map((a) => ({
                                      value: a, label: AGG_LABELS[a] ?? a,
                                    }))} />
                          </Form.Item>
                          <Form.Item noStyle
                                     shouldUpdate={(p, c) => p.measures?.[field.name]?.agg
                                       !== c.measures?.[field.name]?.agg}>
                            {() => {
                              const agg = form.getFieldValue(['measures', field.name, 'agg']);
                              if (agg === 'ratio') {
                                // 聚合比：分子/分母各自 {field, agg}
                                const numAgg = form.getFieldValue(
                                  ['measures', field.name, 'numerator', 'agg']) ?? 'sum';
                                const denAgg = form.getFieldValue(
                                  ['measures', field.name, 'denominator', 'agg']) ?? 'sum';
                                return (
                                  <>
                                    <Form.Item name={[field.name, 'numerator', 'agg']}
                                               initialValue="sum" style={{ marginBottom: 8 }}>
                                      <Select style={{ width: 100 }}
                                              options={['sum', 'avg', 'count', 'min', 'max']
                                                .map((a) => ({ value: a, label: a }))} />
                                    </Form.Item>
                                    <Form.Item name={[field.name, 'numerator', 'field']}
                                               style={{ marginBottom: 8 }}>
                                      <Select showSearch allowClear placeholder="分子字段"
                                              style={{ width: 190 }}
                                              options={fieldOptions} />
                                    </Form.Item>
                                    <span style={{ margin: '0 4px' }}>÷</span>
                                    <Form.Item name={[field.name, 'denominator', 'agg']}
                                               initialValue="sum" style={{ marginBottom: 8 }}>
                                      <Select style={{ width: 100 }}
                                              options={['sum', 'avg', 'count', 'min', 'max']
                                                .map((a) => ({ value: a, label: a }))} />
                                    </Form.Item>
                                    <Form.Item name={[field.name, 'denominator', 'field']}
                                               style={{ marginBottom: 8 }}>
                                      <Select showSearch allowClear placeholder="分母字段"
                                              style={{ width: 190 }}
                                              options={fieldOptions} />
                                    </Form.Item>
                                    <Form.Item name={[field.name, 'scale']}
                                               style={{ marginBottom: 8 }}>
                                      <Select style={{ width: 120 }} allowClear
                                              placeholder="×系数"
                                              options={[
                                                { value: 100, label: '×100（百分数）' },
                                                { value: 1, label: '×1（小数）' },
                                              ]} />
                                    </Form.Item>
                                    <span style={{ fontSize: 12,
                                                   color: 'var(--nb-text-secondary)' }}>
                                      {`（分子 ${numAgg} / 分母 ${denAgg}）`}
                                    </span>
                                  </>
                                );
                              }
                              return (
                                <>
                                  <Form.Item name={[field.name, 'field']}
                                             style={{ marginBottom: 8 }}>
                                    {isForm ? (
                                      <Select
                                        showSearch allowClear
                                        placeholder="选择度量字段（count 可空）"
                                        style={{ width: 250 }}
                                        options={fieldOptions}
                                        onChange={(v) => {
                                          const meta = fieldMetas.find((f) => f.path === v);
                                          if (meta) {
                                            form.setFieldValue(
                                              ['measures', field.name, 'field_type'],
                                              biTypeToFieldType(meta.bi_type));
                                            const cur = form.getFieldValue(
                                              ['measures', field.name, 'key']);
                                            if (!cur) {
                                              const agg2 = form.getFieldValue(
                                                ['measures', field.name, 'agg']) ?? 'sum';
                                              form.setFieldValue(
                                                ['measures', field.name, 'key'],
                                                `${meta.path}_${agg2}`);
                                            }
                                          }
                                        }}
                                      />
                                    ) : (
                                      <Input placeholder="字段/JSON 路径（count 可空）"
                                             style={{ width: 230 }} />
                                    )}
                                  </Form.Item>
                                  <Form.Item name={[field.name, 'field_type']}
                                             style={{ marginBottom: 8 }}>
                                    <Select style={{ width: 190 }} options={FIELD_TYPES} />
                                  </Form.Item>
                                </>
                              );
                            }}
                          </Form.Item>
                          <Button size="small" type="text" danger icon={<DeleteOutlined />}
                                  onClick={() => remove(field.name)} />
                        </Space>
                      ))}
                      <Button size="small" type="dashed" icon={<PlusOutlined />}
                              onClick={() => add({
                                key: '', agg: 'sum', field: undefined,
                                field_type: 'number',
                              })}>
                        添加度量
                      </Button>
                    </>
                  )}
                </Form.List>
              </Form.Item>

              {/* 计算字段（行级派生列；form 源） */}
              {isForm && !isDetail && (
                <Form.Item label="计算字段（行级派生列：引用 [字段名]，可被维度/度量引用）"
                           style={{ marginBottom: 0 }}>
                  <Form.List name="computed">
                    {(cfields, { add: cadd, remove: cremove }) => (
                      <>
                        {cfields.map((cf) => (
                          <Space key={cf.key} align="baseline"
                                 style={{ display: 'flex' }} wrap>
                            <Form.Item name={[cf.name, 'key']}
                                       rules={[{ required: true, message: '标识' }]}
                                       style={{ marginBottom: 8 }}>
                              <Input placeholder="标识，如 rate" style={{ width: 130 }} />
                            </Form.Item>
                            <Form.Item name={[cf.name, 'name']}
                                       style={{ marginBottom: 8 }}>
                              <Input placeholder="显示名，如 合格率"
                                     style={{ width: 140 }} />
                            </Form.Item>
                            <Form.Item name={[cf.name, 'expr']}
                                       rules={[{ required: true, message: '表达式' }]}
                                       style={{ marginBottom: 8 }}>
                              <Input.TextArea rows={1} autoSize={{ minRows: 1, maxRows: 3 }}
                                              placeholder="如 [合格数] / [检验数] * 100"
                                              style={{ width: 320 }} />
                            </Form.Item>
                            <Form.Item name={[cf.name, 'field_type']}
                                       initialValue="number" style={{ marginBottom: 8 }}>
                              <Select style={{ width: 110 }} options={[
                                { value: 'number', label: '数值' },
                                { value: 'string', label: '文本' },
                              ]} />
                            </Form.Item>
                            <span style={{ fontSize: 12, color: 'var(--nb-text-secondary)' }}>
                              可用函数：
                            </span>
                            <Select size="small" style={{ width: 200 }} placeholder="插入函数"
                                    value={null}
                                    options={EXPR_FUNCS.map((f) => ({
                                      value: f, label: f,
                                    }))}
                                    onChange={(f) => {
                                      const cur = form.getFieldValue(
                                        ['computed', cf.name, 'expr']) ?? '';
                                      const fn = String(f).split('(')[0];
                                      form.setFieldValue(['computed', cf.name, 'expr'],
                                        `${cur}${cur ? ' ' : ''}${fn}()`);
                                    }} />
                            <Button size="small" type="text" danger icon={<DeleteOutlined />}
                                    onClick={() => cremove(cf.name)} />
                          </Space>
                        ))}
                        <Button size="small" type="dashed" icon={<PlusOutlined />}
                                onClick={() => cadd({
                                  key: '', name: '', expr: '', field_type: 'number',
                                })}>
                          添加计算字段
                        </Button>
                        <div style={{ fontSize: 12, color: 'var(--nb-text-secondary)',
                                      marginTop: 4 }}>
                          引用写法 [字段中文名]（保存时按字段列表解析）；支持
                          + - * / % 与 round/abs/ceil/floor/min/max/coalesce/
                          substring/upper/lower/trim/concat/date_part。
                        </div>
                      </>
                    )}
                  </Form.List>
                </Form.Item>
              )}

              {/* 展示修饰 */}
              {!isDetail && (
                <Form.Item label="展示修饰" style={{ marginBottom: 0 }}>
                  <Space size={16}>
                    <Form.Item name={['display', 'percent']} valuePropName="checked"
                               noStyle>
                      <Checkbox>占比（柱状/折线按系列总和换算 %）</Checkbox>
                    </Form.Item>
                    <Form.Item name={['display', 'cumulative']} valuePropName="checked"
                               noStyle>
                      <Checkbox disabled={sourceType === 'form'}>
                        累计（按 x 顺序累加；快照源不支持）
                      </Checkbox>
                    </Form.Item>
                  </Space>
                </Form.Item>
              )}

              <Form.Item label="筛选条件（聚合前过滤行，多条需同时满足）" style={{ marginBottom: 0 }}>
                <Form.List name="filters">
                  {(ffields, { add: fadd, remove: fremove }) => (
                    <>
                      {ffields.map((f) => (
                        <Space key={f.key} align="baseline" style={{ display: 'flex' }} wrap>
                          <Form.Item name={[f.name, 'field']}
                                     rules={[{ required: true, message: '选择/填写字段' }]}
                                     style={{ marginBottom: 8 }}>
                            {isForm ? (
                              <Select
                                showSearch allowClear placeholder="选择筛选字段"
                                style={{ width: 220 }} options={fieldOptions}
                                onChange={(v) => {
                                  const meta = fieldMetas.find((x) => x.path === v);
                                  const ft = meta ? meta.bi_type : undefined;
                                  form.setFieldValue(['filters', f.name, 'field_type'], ft);
                                  // 字段类型变化后原操作符可能不再适用：不适用则重置
                                  const cur = form.getFieldValue(['filters', f.name, 'op']);
                                  const allowed = opsForFieldType(ft).map((o) => o.value);
                                  if (cur && !allowed.includes(cur)) {
                                    form.setFieldValue(['filters', f.name, 'op'], 'eq');
                                    form.setFieldValue(['filters', f.name, 'value'], undefined);
                                  }
                                }} />
                            ) : (
                              <Input placeholder="字段路径 / 列名" style={{ width: 200 }} />
                            )}
                          </Form.Item>
                          <Form.Item shouldUpdate noStyle>
                            {() => (
                              <Form.Item name={[f.name, 'op']} initialValue="eq"
                                         style={{ marginBottom: 8 }}>
                                <Select style={{ width: 175 }}
                                        options={opsForFieldType(
                                          form.getFieldValue(['filters', f.name, 'field_type']))} />
                              </Form.Item>
                            )}
                          </Form.Item>
                          <Form.Item shouldUpdate noStyle>
                            {() => {
                              const rule = form.getFieldValue(['filters', f.name]) || {};
                              const op = rule.op || 'eq';
                              const ft = rule.field_type;
                              if (op === 'empty' || op === 'not_empty') return null;
                              if (op === 'in' || op === 'not_in') {
                                return (
                                  <Form.Item name={[f.name, 'value']} style={{ marginBottom: 8 }}>
                                    <Select mode="tags" open={false}
                                            placeholder="输入后回车，可多个"
                                            style={{ width: 230 }} />
                                  </Form.Item>
                                );
                              }
                              if (op === 'between' || op === 'not_between') {
                                const num = ft === 'number';
                                return (
                                  <Space.Compact>
                                    <Form.Item name={[f.name, 'value', 0]} noStyle>
                                      {num
                                        ? <InputNumber placeholder="最小" style={{ width: 110 }} />
                                        : <Input placeholder="起始" style={{ width: 125 }} />}
                                    </Form.Item>
                                    <Form.Item name={[f.name, 'value', 1]} noStyle>
                                      {num
                                        ? <InputNumber placeholder="最大" style={{ width: 110 }} />
                                        : <Input placeholder="终止" style={{ width: 125 }} />}
                                    </Form.Item>
                                  </Space.Compact>
                                );
                              }
                              return (
                                <Form.Item name={[f.name, 'value']} style={{ marginBottom: 8 }}>
                                  {ft === 'number'
                                    ? <InputNumber placeholder="数值" style={{ width: 170 }} />
                                    : <Input placeholder="值" style={{ width: 190 }} />}
                                </Form.Item>
                              );
                            }}
                          </Form.Item>
                          <Button size="small" type="text" danger icon={<DeleteOutlined />}
                                  onClick={() => fremove(f.name)} />
                        </Space>
                      ))}
                      <Button size="small" type="dashed" icon={<PlusOutlined />}
                              onClick={() => fadd({ field: '', op: 'eq' })}>
                        添加筛选条件
                      </Button>
                    </>
                  )}
                </Form.List>
              </Form.Item>
            </>
          )}

          {/* 列显示名（明细卡：卡级别名，优先于字段字典的全局名）——
              置于 !isDetail 大块之外（detail 时维度/度量整块隐藏，本区仅 detail 显示） */}
          {isForm && isDetail && (
            <Form.Item label="列显示名（大屏表头/排序下拉使用；留空回落字段字典或原名）"
                       style={{ marginBottom: 0 }}>
              {fieldMetas.length === 0 ? (
                <div style={{ fontSize: 12, color: 'var(--nb-text-secondary)' }}>
                  选择连接与表单后加载字段
                </div>
              ) : (
                <div style={{
                  maxHeight: 220, overflow: 'auto', padding: '8px 10px',
                  border: '1px solid var(--nb-border, #eee)', borderRadius: 6,
                }}>
                  {fieldMetas.map((f) => (
                    <div key={f.path} style={{ display: 'flex', alignItems: 'center',
                                               gap: 8, marginBottom: 6 }}>
                      <span style={{ width: 230, fontSize: 12, flexShrink: 0,
                                     color: 'var(--nb-text-secondary)',
                                     overflow: 'hidden', textOverflow: 'ellipsis',
                                     whiteSpace: 'nowrap' }}>
                        {f.name}（{f.path}）
                      </span>
                      <Form.Item name={['style', 'column_labels', f.path]}
                                 style={{ marginBottom: 0, flex: 1 }}>
                        <Input size="small" allowClear
                               placeholder={`显示名（默认 ${f.name}）`} />
                      </Form.Item>
                    </div>
                  ))}
                </div>
              )}
            </Form.Item>
          )}

          <div style={{ marginTop: 8 }}>
            <Button size="small" onClick={runEstimate}>预估维度组合数</Button>
          </div>
        </Form>
      </Modal>

      {/* 维度分组编辑（值 → 组；未命中归"其他"，留空则保留原值） */}
      <Modal
        title="自定义分组（把维度值归类；如 A线/B线 → 一车间）"
        open={groupEdit !== null}
        okText="保存" cancelText="取消"
        onCancel={() => setGroupEdit(null)}
        onOk={() => {
          if (!groupEdit) return;
          const groups = groupEdit.rows
            .filter((r) => r.name.trim())
            .map((r) => ({
              name: r.name.trim(),
              values: r.values.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean),
            }));
          form.setFieldValue(['dimensions', groupEdit.idx, 'groups'],
            groups.length ? groups : undefined);
          form.setFieldValue(['dimensions', groupEdit.idx, 'other'],
            groupEdit.other.trim() || undefined);
          setGroupEdit(null);
        }}
        width={520}
      >
        {groupEdit && (
          <div>
            {(groupEdit.rows ?? []).map((row, i) => (
              <Space key={i} align="baseline" style={{ display: 'flex', marginBottom: 8 }}>
                <Input placeholder="组名，如一车间" style={{ width: 140 }}
                       value={row.name}
                       onChange={(e) => {
                         const rows = [...groupEdit.rows];
                         rows[i] = { ...rows[i], name: e.target.value };
                         setGroupEdit({ ...groupEdit, rows });
                       }} />
                <span style={{ fontSize: 12 }}>值（逗号分隔）</span>
                <Input placeholder="A线, B线" style={{ width: 240 }}
                       value={row.values}
                       onChange={(e) => {
                         const rows = [...groupEdit.rows];
                         rows[i] = { ...rows[i], values: e.target.value };
                         setGroupEdit({ ...groupEdit, rows });
                       }} />
                <Button size="small" type="text" danger icon={<DeleteOutlined />}
                        onClick={() => setGroupEdit({
                          ...groupEdit,
                          rows: groupEdit.rows.filter((_, j) => j !== i),
                        })} />
              </Space>
            ))}
            <Button size="small" type="dashed" icon={<PlusOutlined />}
                    onClick={() => setGroupEdit({
                      ...groupEdit, rows: [...groupEdit.rows, { name: '', values: '' }],
                    })}>
              添加分组
            </Button>
            <div style={{ marginTop: 10 }}>
              <Space>
                <span style={{ fontSize: 12 }}>其余值归为</span>
                <Input placeholder="留空 = 保留原值" style={{ width: 200 }}
                       value={groupEdit.other}
                       onChange={(e) => setGroupEdit({ ...groupEdit, other: e.target.value })} />
              </Space>
            </div>
            <div style={{ marginTop: 8, fontSize: 12, color: 'var(--nb-text-secondary)' }}>
              分组后大屏按组名聚合；值可用逗号分隔多个（如「A线, B线」）。
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
};

export default BiDefinitionsPage;
