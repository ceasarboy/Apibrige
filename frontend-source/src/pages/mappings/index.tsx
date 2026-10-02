import React, { useEffect, useState } from 'react';
import { isAxiosError } from 'axios';
import {
  Card, Table, Button, Select, Input, Modal, Form, Space, AutoComplete, InputNumber, Alert,
  message, Divider, Row, Col, Checkbox, Popconfirm } from 'antd';
import {
  PlusOutlined, ReloadOutlined, LinkOutlined, DeleteOutlined,
  ArrowRightOutlined,
} from '@ant-design/icons';
import { useMappingStore, Mapping } from '../../stores/mappingStore';
import apiClient from '../../api/client';
import ContractEditor, { JsonInput } from './ContractEditor';
import SourceReadEditor from './SourceReadEditor';
import { resolveTargetKey, targetKeyOptions } from './targetKey';

interface FieldDef {
  path: string;
  name: string;
  type: string;
  nullable?: boolean;
  primary_key?: boolean;
  is_array?: boolean;
  field_id?: string;
  alias?: string;
  write_path?: string;
  read_only?: boolean;
}

interface FieldPair {
  source: string;
  target: string;
  transform: string;
  source_ref?: { field_id: string; alias: string };
  on_missing?: 'error' | 'null' | 'default';
  default_value?: unknown;
}

const TRANSFORM_OPTIONS = [
  { label: '原样 (string)', value: 'string' },
  { label: '转整数 (int)', value: 'int' },
  { label: '转小数 (float)', value: 'float' },
];

const SYNC_MODE_OPTIONS = [
  { label: '全量', value: 'full' },
  { label: '增量（按水位）', value: 'incremental' },
];

const MappingsPage: React.FC = () => {
  const { mappings, loading, fetchMappings, createMapping, updateMapping, deleteMapping } =
    useMappingStore();
  const [registeredForms, setRegisteredForms] = useState<any[]>([]);
  const [connections, setConnections] = useState<any[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // 表单态
  const [name, setName] = useState('');
  const [srcConnId, setSrcConnId] = useState<string>();
  const [tgtConnId, setTgtConnId] = useState<string>();
  const [srcEndpoint, setSrcEndpoint] = useState('');
  const [tgtTable, setTgtTable] = useState('');
  const [syncMode, setSyncMode] = useState('full');
  const [incrementalField, setIncrementalField] = useState('');
  const [sourceReadValid, setSourceReadValid] = useState(true);
  const [batchSize, setBatchSize] = useState(1000);
  const [pairs, setPairs] = useState<FieldPair[]>([]);
  const [srcFields, setSrcFields] = useState<FieldDef[]>([]);
  const [tgtFields, setTgtFields] = useState<FieldDef[]>([]);
  const [fieldsHint, setFieldsHint] = useState('');
  const [fetchingFields, setFetchingFields] = useState(false);
  const [keyField, setKeyField] = useState('');
  const [deleteSync, setDeleteSync] = useState(false);
  const [fullConfig, setFullConfig] = useState<Record<string, any>>({});
  const [targetEndpoint, setTargetEndpoint] = useState('');
  const [mappingHeaders, setMappingHeaders] = useState<any>({ source_headers:null, target_headers:null });
  const [configValid, setConfigValid] = useState(true);
  const [headersValid, setHeadersValid] = useState(true);
  const [sampleText, setSampleText] = useState('[{}]');
  const [preview, setPreview] = useState<any>(null);
  const [previewing, setPreviewing] = useState(false);

  useEffect(() => { fetchMappings(); }, [fetchMappings]);

  useEffect(() => {
    apiClient.get('/connections').then(({ data }) => setConnections(data));
    apiClient.get('/forms').then(({ data }) => setRegisteredForms(data)).catch(() => setRegisteredForms([]));
  }, []);

  const connName = (id?: string) =>
    connections.find(c => c.id === id)?.name || id || '-';

  // 拉取两侧字段定义（DB 按表、HAP 按工作表、REST 提示手填）
  // 可传入连接 ID / 端点覆盖：编辑回填时 state 尚未更新，直接用映射里的值
  const fetchFields = async (
    overrideSrc?: string,
    overrideTgt?: string,
    overrideSrcEp?: string,
    overrideTgtTbl?: string,
  ) => {
    const srcId = overrideSrc ?? srcConnId;
    const tgtId = overrideTgt ?? tgtConnId;
    const srcEp = overrideSrcEp ?? srcEndpoint;
    const tgtTbl = overrideTgtTbl ?? tgtTable;
    if (!srcId || !tgtId) {
      message.warning('请先选择源连接与目标连接');
      return;
    }
    setFetchingFields(true);
    setFieldsHint('');
    try {
      // DB 侧认 table 参数，HAP 侧认 worksheet_id——同时都传，各取所需
      const results = await Promise.allSettled([
        apiClient.get(`/connections/${srcId}/fields`, {
          params: { table: srcEp || undefined, worksheet_id: srcEp || undefined },
        }),
        apiClient.get(`/connections/${tgtId}/fields`, {
          params: { table: tgtTbl || undefined, worksheet_id: tgtTbl || undefined },
        }),
      ]);
      const hints: string[] = [];
      results.forEach((result, index) => {
        const side = index === 0 ? '源端' : '目标端';
        const setFields = index === 0 ? setSrcFields : setTgtFields;
        if (result.status === 'fulfilled') {
          const fields: FieldDef[] = result.value.data.fields || [];
          setFields(index === 0 ? fields : fields.filter(f => !f.read_only));
          if (!result.value.data.fields?.length) {
            hints.push(`${side}：${result.value.data.hint || '无自动字段定义'}`);
          }
        } else {
          setFields([]);
          const detail = isAxiosError(result.reason) ? result.reason.response?.data?.detail : undefined;
          const error = typeof detail === 'string' ? detail : detail?.message;
          hints.push(`${side}：${error || '字段定义读取失败，请检查连接状态。'}`);
        }
      });
      setFieldsHint(hints.join('；'));
    } finally {
      setFetchingFields(false);
    }
  };

  const openCreate = () => {
    setFullConfig({ version:2, protocol:{ source:{}, target:{} }, mapping_options:{ missing_source:'error', conversion_error:'error' } });
    setTargetEndpoint(''); setMappingHeaders({source_headers:null,target_headers:null}); setPreview(null); setConfigValid(true); setHeadersValid(true);
    setEditingId(null);
    setName('');
    setSrcConnId(undefined);
    setTgtConnId(undefined);
    setSrcEndpoint('');
    setTgtTable('');
    setSyncMode('full');
    setIncrementalField(''); setBatchSize(1000);
    setPairs([]);
    setSrcFields([]);
    setTgtFields([]);
    setFieldsHint('');
    setKeyField('');
    setDeleteSync(false);
    setModalOpen(true);
  };

  const openEdit = (m: Mapping) => {
    setFullConfig(structuredClone(m.mapping_config));
    setTargetEndpoint(m.target_endpoint || '');
    setMappingHeaders({ source_headers:(m as any).source_headers ?? null, target_headers:(m as any).target_headers ?? null });
    setPreview(null); setConfigValid(true); setHeadersValid(true);
    setEditingId(m.id);
    setName(m.name);
    setSrcConnId(m.source_connection_id);
    setTgtConnId(m.target_connection_id);
    setSrcEndpoint((m.mapping_config as any).protocol?.source?.worksheet_id || m.source_endpoint || m.source_table || '');
    setTgtTable((m.mapping_config as any).protocol?.target?.worksheet_id || (m.mapping_config as any).protocol?.worksheet_id || m.target_table || '');
    setSyncMode(m.sync_mode || 'full');
    setIncrementalField(m.incremental_field || ''); setBatchSize(m.batch_size || 1000);
    const cfg = m.mapping_config as any;
    setPairs((cfg?.fields || []).map((f: any) => ({
      ...f, source: f.source, target: f.target,
    })));
    setKeyField(cfg?.key_field || '');
    setDeleteSync(!!cfg?.delete_sync);
    setSrcFields([]);
    setTgtFields([]);
    setFieldsHint('');
    setModalOpen(true);
    // 打开即拉取两侧字段定义，让映射对显示为 名称（ID）而非裸 ID
    fetchFields(m.source_connection_id, m.target_connection_id,
      (m.mapping_config as any).protocol?.source?.worksheet_id || m.source_endpoint || m.source_table || '',
      (m.mapping_config as any).protocol?.target?.worksheet_id || (m.mapping_config as any).protocol?.worksheet_id || m.target_table || '');
  };

  const configurationForSave = () => {
    const cfg = structuredClone(fullConfig);
    const old = mappings.find(m => m.id === editingId);
    for (const [side, id, formKey] of [['source', srcConnId, srcEndpoint], ['target', tgtConnId, tgtTable]] as const) {
      if (connections.find(c => c.id === id)?.protocol !== 'hap') continue;
      const prev = side === 'source' ? old?.source_endpoint || old?.source_table : old?.target_table;
      const protocol = cfg.version === 2 ? cfg.protocol?.[side] : cfg.protocol;
      if (formKey && (!old || formKey !== (protocol?.worksheet_id || prev || ''))) {
        cfg.protocol ||= {};
        if (cfg.version === 2) cfg.protocol[side] = { ...cfg.protocol[side], worksheet_id: formKey };
        else cfg.protocol.worksheet_id = formKey;
      }
    }
    return cfg;
  };

  const endpointsForSave = () => {
    const old = mappings.find(m => m.id === editingId);
    const oldCfg = old?.mapping_config as any;
    const oldSrc = oldCfg?.protocol?.source?.worksheet_id || old?.source_endpoint || old?.source_table || '';
    const oldTgt = oldCfg?.protocol?.target?.worksheet_id || oldCfg?.protocol?.worksheet_id || old?.target_table || '';
    const sourceUnchanged = old && old.source_connection_id === srcConnId && oldSrc === srcEndpoint;
    const targetUnchanged = old && old.target_connection_id === tgtConnId && oldTgt === tgtTable;
    return {
      source_endpoint: sourceUnchanged ? old.source_endpoint : srcEndpoint || null,
      source_table: sourceUnchanged ? old.source_table : srcEndpoint || null,
      target_table: targetUnchanged ? old.target_table : tgtTable || null,
    };
  };

  const hapUpsert = connections.find(c => c.id === tgtConnId)?.protocol === 'hap' && fullConfig.hap_write_mode === 'upsert';
  const effectiveKey = hapUpsert ? resolveTargetKey(keyField, tgtFields, pairs) : keyField || undefined;
  const keyOptions = hapUpsert ? targetKeyOptions(tgtFields, pairs) : tgtFields.map(f => ({
    label: f.name && f.name !== f.path ? `${f.name}（${f.path}）` : f.path, value: f.path,
  }));

  const currentBody = (): Record<string, unknown> => ({
    name:name.trim(), source_connection_id:srcConnId, target_connection_id:tgtConnId,
    ...endpointsForSave(), target_endpoint:targetEndpoint || null,
    ...mappingHeaders,
    mapping_config:{ ...configurationForSave(), fields:pairs.filter(p => p.source && p.target), key_field:effectiveKey, delete_sync:deleteSync || undefined },
    sync_mode:syncMode,
    incremental_field: syncMode === 'incremental' ? incrementalField.trim() : null,
    batch_size: batchSize,
  });

  const hapBulk = (fullConfig.version === 2 ? fullConfig.protocol?.target : fullConfig.protocol)?.hap_bulk || {};
  const changeHapBulk = (patch: Record<string, any>) => {
    if (fullConfig.version === 2) setFullConfig({ ...fullConfig, protocol: { ...fullConfig.protocol,
      target: { ...fullConfig.protocol?.target, hap_bulk: { ...hapBulk, ...patch } } } });
    else setFullConfig({ ...fullConfig, protocol: { ...fullConfig.protocol, hap_bulk: { ...hapBulk, ...patch } } });
  };
  const previewHap = async () => {
    if (!editingId) return;
    setPreviewing(true);
    try {
      const { data } = await apiClient.post(`/mappings/${editingId}/hap-preview`, {test_data:JSON.parse(sampleText), draft:currentBody()});
      setPreview(data);
    } catch { message.error('预览失败，请检查样本、目标字段和业务键'); }
    finally { setPreviewing(false); }
  };

  const previewRequest = async () => {
    if (!editingId || !configValid || !headersValid) return;
    setPreviewing(true);
    try {
      const { data } = await apiClient.post(`/mappings/${editingId}/request-preview`, { test_data:JSON.parse(sampleText), draft:currentBody() });
      setPreview(data);
    } catch (e: any) { message.error(e.response?.data?.detail?.message || '预览失败，请检查样本和配置格式'); }
    finally { setPreviewing(false); }
  };

  const save = async () => {
    if (!sourceReadValid) { message.error('请检查 REST 源读取、分页及增量配置'); return; }
    if (syncMode === 'incremental') {
      if (!incrementalField.trim()) { message.error('请选择或填写源端增量字段'); return; }
      const source = connections.find(c => c.id === srcConnId);
      if (!source?.db_type && !['hap', 'rest', 'http'].includes(source?.protocol || '')) {
        message.error('增量支持数据库、HAP 和 REST 源'); return;
      }
    }
    if (!configValid || !headersValid) { message.error('请先修正 JSON 配置'); return; }
    if (!name.trim()) { message.error('请输入名称'); return; }
    if (!srcConnId || !tgtConnId) { message.error('请选择源/目标连接'); return; }
    const validPairs = pairs.filter(p => p.source && p.target);
    if (validPairs.length === 0) { message.error('请至少配置一条字段映射'); return; }
    if (hapUpsert && (!effectiveKey || !validPairs.some(p => p.target === effectiveKey))) {
      message.error('请在主键字段中选择一个已经配置源字段的目标字段');
      return;
    }

    const body = currentBody();
    try {
      setSubmitting(true);
      if (editingId) {
        await updateMapping(editingId, body);
        message.success('映射已更新');
      } else {
        await createMapping(body);
        message.success('映射已创建');
      }
      setModalOpen(false);
      fetchMappings();
    } catch (e: any) {
      const detail = e.response?.data?.detail;
      const validation = Array.isArray(detail)
        ? detail.map((item: any) => `${Array.isArray(item.loc) ? item.loc.join('.') : '配置'}：${item.msg || '无效'}`).join('；')
        : undefined;
      message.error(typeof detail === 'string' ? detail : detail?.message || validation || '保存失败，请检查字段与请求配置');
    } finally {
      setSubmitting(false);
    }
  };

  const updatePair = (idx: number, patch: Partial<FieldPair>) => {
    setPairs(prev => prev.map((p, i) => (i === idx ? { ...p, ...patch } : p)));
  };

  const modalTitle = editingId ? '编辑字段映射' : '新建字段映射';

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
        <h1 className="page-heading" style={{ marginBottom: 0 }}>字段映射</h1>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => fetchMappings()}>刷新</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新建映射</Button>
        </Space>
      </div>

      <Card bordered style={{ borderRadius: 'var(--nb-radius-lg)', border: '1px solid var(--nb-border)', boxShadow: 'var(--nb-shadow)' }}>
        <Table
          rowKey="id"
          columns={[
            { title: '名称', dataIndex: 'name', width: 180 },
            {
              title: '源 → 目标连接',
              render: (_, r: Mapping) =>
                `${connName(r.source_connection_id)} → ${connName(r.target_connection_id)}`,
            },
            {
              title: '源端点/表',
              render: (_, r: Mapping) => r.source_endpoint || r.source_table || '-',
            },
            { title: '目标表', dataIndex: 'target_table', render: (v) => v || '-' },
            {
              title: '映射字段数',
              render: (_, r: Mapping) => {
                const cfg = r.mapping_config as any;
                return (cfg?.fields || []).length;
              },
            },
            {
              title: '操作',
              width: 160,
              render: (_, r: Mapping) => (
                <Space>
                  <Button size="small" onClick={() => openEdit(r)}>编辑</Button>
                  <Popconfirm title="确认删除该字段映射？" onConfirm={async () => {
                    try {
                      await deleteMapping(r.id);
                    } catch { /* 409（被任务引用）由拦截器提示 */ }
                    fetchMappings();
                  }}>
                    <Button size="small" danger>删除</Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
          dataSource={mappings}
          loading={loading}
          pagination={{ pageSize: 10 }}
        />
      </Card>

      <Modal
        title={modalTitle}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        width={900}
        footer={[
          <Button key="cancel" onClick={() => setModalOpen(false)}>取 消</Button>,
          <Button key="save" type="primary" loading={submitting} onClick={save}>
            保存映射
          </Button>,
        ]}
      >
        <Form layout="vertical">
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item label="名称" required>
                <Input value={name} onChange={e => setName(e.target.value)}
                  placeholder="映射名称" maxLength={100} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item label="同步模式">
                <Select value={syncMode} onChange={v => { setSyncMode(v); if (v === 'incremental') { setDeleteSync(false); setFullConfig(c => ({...c,hap_delete_sync:false})); } }} options={SYNC_MODE_OPTIONS} />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item label="源连接" required>
                <Select showSearch value={srcConnId}
                  onChange={(v) => { setSrcConnId(v); setSrcFields([]); setIncrementalField(''); }}
                  placeholder="选择源连接"
                  options={connections.map(c => ({ label: `${c.name} (${c.protocol})`, value: c.id }))} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item label="目标连接" required>
                <Select showSearch value={tgtConnId}
                  onChange={(v) => { setTgtConnId(v); setTgtFields([]); }}
                  placeholder="选择目标连接"
                  options={connections.map(c => ({ label: `${c.name} (${c.protocol})`, value: c.id }))} />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item label="源端点 / 工作表 ID">
                <AutoComplete value={srcEndpoint} onChange={value => { setSrcEndpoint(value); setSrcFields([]); setIncrementalField(''); }}
                  options={registeredForms.filter(f => f.connection_id === srcConnId).map(f => ({ value: f.form_key, label: `${f.display_name || f.form_key}（${f.form_key}）` }))}
                  filterOption={(input, option) => String(option?.label).toLowerCase().includes(input.toLowerCase())}
                  placeholder="REST 路径或明道云 worksheetId" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item label="目标表 / 工作表 ID">
                <AutoComplete value={tgtTable} onChange={setTgtTable}
                  options={registeredForms.filter(f => f.connection_id === tgtConnId).map(f => ({ value: f.form_key, label: `${f.display_name || f.form_key}（${f.form_key}）` }))}
                  filterOption={(input, option) => String(option?.label).toLowerCase().includes(input.toLowerCase())}
                  placeholder="Oracle/MySQL 表名或明道云 worksheetId" />
              </Form.Item>
            </Col>
          </Row>

          {syncMode === 'incremental' && <>
            <Alert type="info" showIcon message="首次同步已有数据，之后按已提交水位筛选新增和变更。整轮全部成功后推进水位；异步需 RabbitMQ、Worker 和补偿调度。" />
            <Row gutter={12} style={{ marginTop: 12 }}>
              <Col span={16}><Form.Item label="增量字段（源端）" required extra="选择非空数字或日期时间字段。数据库源需要主键，HAP 使用 rowid，REST 主键在下方配置。">
                <AutoComplete value={incrementalField} onChange={setIncrementalField}
                  options={srcFields.filter(f => /int|number|numeric|decimal|real|float|double|date|time/i.test(f.type)).map(f => ({ value: f.path, label: `${f.name}（${f.type}）` }))}
                  filterOption={(input, option) => String(option?.value).toLowerCase().includes(input.toLowerCase())}
                  placeholder="例如 updated_at 或 id" />
              </Form.Item></Col>
              <Col span={8}><Form.Item label="每批记录数"><InputNumber min={1} max={100000} value={batchSize} onChange={v => setBatchSize(v || 1000)} /></Form.Item></Col>
            </Row>
            {connections.find(c => c.id === srcConnId)?.protocol === 'hap' && <Alert type="info" showIcon style={{ marginBottom: 12 }}
              message="HAP 推荐使用 utime（更新时间），可同步新增和修改；ctime 主要用于新增。每轮完整分页读取两次，结果一致后按水位筛选推送；这不是数据库事务快照，同水位迟到、旧时间回填和删除仍无法捕获。" />}
          </>}
          {connections.find(c => c.id === srcConnId)?.protocol === 'hap' && fullConfig.version === 2 && <>
            <Form.Item label="HAP 视图 ID（可选）" extra="明道云侧先过滤数据时填写；视图需提供稳定排序。">
              <Input value={fullConfig.protocol?.source?.view_id || ''} placeholder="worksheet viewId"
                onChange={e => setFullConfig({ ...fullConfig, protocol: { ...fullConfig.protocol,
                  source: { ...fullConfig.protocol?.source, view_id: e.target.value || null } } })} />
            </Form.Item>
            <JsonInput label="HAP 源过滤条件（V2 filters 数组，值支持 {{last_watermark}}）"
              value={fullConfig.protocol?.source?.filters || []}
              onChange={filters => setFullConfig({ ...fullConfig, protocol: { ...fullConfig.protocol,
                source: { ...fullConfig.protocol?.source, filters } } })}
              onValid={v => setConfigValid(v)} />
          </>}
          {['rest', 'http'].includes(connections.find(c => c.id === srcConnId)?.protocol || '') &&
            <SourceReadEditor config={fullConfig} onChange={setFullConfig} onValid={setSourceReadValid} incrementalMode={syncMode === 'incremental'} />}
          <Divider orientation="left" plain>
            <Space>
              <LinkOutlined />
              字段映射（左源 → 右目标）
              <Button size="small" icon={<ReloadOutlined />} loading={fetchingFields}
                onClick={() => fetchFields()}>
                获取字段
              </Button>
            </Space>
          </Divider>

          {fieldsHint && (
            <div style={{ marginBottom: 10, fontSize: 12, color: '#faad14' }}>{fieldsHint}</div>
          )}

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 32px 1fr', gap: 8, marginBottom: 8 }}>
            <div style={{ fontWeight: 600, fontSize: 13 }}>源字段</div>
            <div />
            <div style={{ fontWeight: 600, fontSize: 13 }}>目标字段</div>
          </div>

          {pairs.map((p, idx) => (
            <div key={idx} style={{
              display: 'grid', gridTemplateColumns: '1fr 32px 1fr 110px 32px',
              gap: 8, marginBottom: 8, alignItems: 'center',
            }}>
              {srcFields.length ? <Select showSearch value={p.source || undefined}
                onChange={(v) => { const f = srcFields.find(f => f.path === v); updatePair(idx, { source: v, source_ref:f?.field_id ? { field_id:f.field_id, alias:f.alias || '' } : undefined }); }}
                placeholder="源字段"
                options={srcFields.map(f => ({
                  label: f.name && f.name !== f.path ? `${f.name}（${f.path}）` : f.path,
                  value: f.path,
                }))} /> : <Input value={p.source} placeholder="源字段"
                  onChange={e => updatePair(idx, { source: e.target.value })} />}
              <ArrowRightOutlined style={{ color: '#999' }} />
              {tgtFields.length ? <Select showSearch value={p.target || undefined}
                onChange={(v) => updatePair(idx, { target: v })}
                placeholder="目标字段"
                options={tgtFields.map(f => ({
                  label: f.name && f.name !== f.path ? `${f.name}（${f.path}）` : f.path,
                  value: f.write_path || f.path,
                }))} /> : <Input value={p.target} placeholder="目标字段"
                  onChange={e => updatePair(idx, { target: e.target.value })} />}
              <Select value={p.transform} size="middle"
                onChange={(v) => updatePair(idx, { transform: v })}
                options={TRANSFORM_OPTIONS} />
              <Button icon={<DeleteOutlined />} danger size="small"
                onClick={() => setPairs(prev => prev.filter((_, i) => i !== idx))} />
            </div>
          ))}

          <Space style={{ marginBottom: 16 }}>
            <Button icon={<PlusOutlined />}
              onClick={() => setPairs(prev => [...prev, { source: '', target: '', transform: 'string' }])}>
              添加映射
            </Button>
          </Space>

          <Form.Item extra="只限制映射输出；旧映射默认关闭。保存字典后，新执行采用新规则，已提交批次保留原规则。">
            <Checkbox checked={fullConfig.field_policy === 'inherit'} onChange={e => setFullConfig({ ...fullConfig, field_policy: e.target.checked ? 'inherit' : 'off' })}>启用字段字典过滤</Checkbox>
          </Form.Item>
          {connections.find(c => c.id === tgtConnId)?.protocol === 'hap' && <>
            <Form.Item label="HAP 写入方式" extra="去重模式跳过已有键；新增更新模式支持全量/增量 sync 或 async，按业务键写入变更。">
              <Select value={fullConfig.hap_write_mode || 'append'} onChange={v => setFullConfig({ ...fullConfig, hap_write_mode: v })}
                options={[{value:'append',label:'原追加模式'},{value:'deduplicate',label:'按业务键去重'},{value:'upsert',label:'新增 + 更新（批量）'}]} />
            </Form.Item>
            {fullConfig.hap_write_mode === 'upsert' && <>
              <Alert type="info" showIcon message="新增使用 addRows；更新按相同变更值分组使用 editRows，不同值可能分别请求。失败后恢复原计划，未知请求只回查，不直接重发。" />
              <Row gutter={12} style={{ marginTop: 12 }}>
                <Col span={12}><Form.Item label="每次批量新增条数" extra="默认 100 为项目保守值，实际限额需按目标部署确认。">
                  <InputNumber aria-label="每次批量新增条数" min={1} max={1000} value={hapBulk.create_batch_size ?? 100} onChange={v => changeHapBulk({create_batch_size:v})} />
                </Form.Item></Col>
                <Col span={12}><Form.Item label="每次同值更新条数">
                  <InputNumber aria-label="每次同值更新条数" min={1} max={1000} value={hapBulk.update_batch_size ?? 100} onChange={v => changeHapBulk({update_batch_size:v})} />
                </Form.Item></Col>
              </Row>
              <Form.Item label="源 null 值处理">
                <Select value={hapBulk.null_policy || 'omit'} onChange={v => changeHapBulk({null_policy:v})} options={[{value:'omit',label:'不更新该字段'},{value:'clear',label:'清空目标字段'}]} />
              </Form.Item>
              <Form.Item extra="默认关闭。开启后超时请求的字段值一致也无法证明工作流是否执行，计划将等待人工核对。">
                <Checkbox checked={!!hapBulk.trigger_workflow} onChange={e => changeHapBulk({trigger_workflow:e.target.checked})}>触发明道云工作流</Checkbox>
              </Form.Item>
              <Alert type="warning" message="支持文本、数值、日期和单选字段；单选按已有选项名称或 key 精确匹配，不自动创建选项。多选等其他复杂控件仍会拒绝。目标业务键必须唯一且不可变。" />
            </>}
            <Form.Item extra="需完整源集合和目标业务键；空源不清空目标。旧删除同步开关不会自动启用此能力。">
              <Checkbox disabled={syncMode === 'incremental'} checked={!!fullConfig.hap_delete_sync} onChange={e => setFullConfig({ ...fullConfig, hap_delete_sync: e.target.checked })}>启用 HAP 全量删除对账</Checkbox>
            </Form.Item>
          </>}
          {fullConfig.hap_write_mode === 'upsert' && <Button disabled={!editingId} loading={previewing} onClick={previewHap}>
            查询 HAP 目标并预览新增/修改（使用下方样本，零写入）
          </Button>}
          <Divider orientation="left" plain>接口契约</Divider>
          <Form.Item label="目标 REST 端点"><Input value={targetEndpoint} onChange={e => setTargetEndpoint(e.target.value)} placeholder="/thirdparty/mingdao/material/sync" /></Form.Item>
          <ContractEditor config={fullConfig} onChange={setFullConfig} onValid={setConfigValid} isRest={['rest','http'].includes(connections.find(c => c.id === tgtConnId)?.protocol)} />
          <details style={{ margin:'12px 0' }}><summary>导入完整映射配置</summary>
            <Input.TextArea aria-label="完整映射配置 JSON" rows={6} placeholder="粘贴已校验的 mapping_config JSON" id="mapping-config-import" />
            <Button onClick={() => {
              try {
                const raw = (document.getElementById('mapping-config-import') as HTMLTextAreaElement).value;
                const next = JSON.parse(raw);
                if (!next || !Array.isArray(next.fields) || next.fields.some((f: any) => !f.source || !f.target)) throw Error();
                setFullConfig(next); setPairs(next.fields); setKeyField(next.key_field || ''); setDeleteSync(!!next.delete_sync);
                setConfigValid(true); setPreview(null); message.success('配置已载入表单，请预览后保存');
              } catch { message.error('请输入包含 fields 数组的有效映射配置'); }
            }}>载入表单（不保存）</Button>
          </details>
          <JsonInput label="映射级请求头（source_headers / target_headers）" value={mappingHeaders} onChange={setMappingHeaders} onValid={setHeadersValid} />
          <Divider orientation="left" plain>请求预览（不写入目标）</Divider>
          <Input.TextArea aria-label="预览源样本" rows={4} value={sampleText} onChange={e => setSampleText(e.target.value)} />
          <Button style={{ margin:'12px 0' }} loading={previewing} disabled={!editingId || !configValid || !headersValid} onClick={previewRequest}>预览当前配置</Button>
          {!editingId && <div>保存映射后可使用服务端请求预览。</div>}
          {preview && <pre style={{ maxHeight:320, overflow:'auto', whiteSpace:'pre-wrap' }}>{JSON.stringify(preview,null,2)}</pre>}
          <Divider orientation="left" plain>同步选项</Divider>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item label="主键字段（目标侧；用于 upsert 与删除同步）"
                extra={hapUpsert ? '从上方已完成映射的目标字段中选择；字段 ID 与别名会按元数据对应。' : undefined}>
                {tgtFields.length || hapUpsert ? <Select showSearch allowClear value={effectiveKey}
                  onChange={setKeyField} placeholder="选择目标主键字段"
                  options={keyOptions} /> : <Input value={keyField} placeholder="目标主键字段"
                    onChange={e => setKeyField(e.target.value)} />}
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item label="删除同步">
                <Checkbox checked={deleteSync} disabled={syncMode === 'incremental'}
                  onChange={e => setDeleteSync(e.target.checked)}>
                  源侧已删除的行，同步时自动从目标删除（仅全量模式）
                </Checkbox>
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>
    </div>
  );
};

export default MappingsPage;
