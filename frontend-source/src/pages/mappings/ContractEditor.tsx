import { useEffect, useState } from 'react';
import { Alert, Button, Checkbox, Col, Form, Input, InputNumber, Modal, Row, Select, Space, Tabs } from 'antd';

export function JsonInput({ value, onChange, onValid, label }: {
  value: unknown; onChange: (value: any) => void; onValid?: (valid: boolean) => void; label: string;
}) {
  const [text, setText] = useState(JSON.stringify(value, null, 2));
  const [error, setError] = useState('');
  const serialized = JSON.stringify(value, null, 2);
  useEffect(() => { setText(serialized); setError(''); onValid?.(true); }, [serialized]);
  useEffect(() => () => onValid?.(true), []);
  return <Form.Item label={label} validateStatus={error ? 'error' : undefined} help={error}>
    <Input.TextArea value={text} rows={5} aria-label={label} onChange={e => {
      setText(e.target.value);
      try { const parsed = JSON.parse(e.target.value); setError(''); onValid?.(true); onChange(parsed); }
      catch { setError('请输入合法 JSON'); onValid?.(false); }
    }} />
  </Form.Item>;
}

export default function ContractEditor({ config, onChange, onValid, isRest }: {
  config: Record<string, any>; onChange: (c: Record<string, any>) => void;
  onValid: (valid: boolean) => void; isRest: boolean;
}) {
  const [validity, setValidity] = useState<Record<string, boolean>>({});
  const valid = (key: string, value: boolean) => setValidity(v => v[key] === value ? v : { ...v, [key]: value });
  useEffect(() => { onValid(Object.values(validity).every(Boolean)); }, [validity]);
  const target = config.protocol?.target || {};
  const request = target.request || {};
  const response = target.response || {};
  const changeTarget = (value: Record<string, any>) => onChange({ ...config, protocol: { ...config.protocol, target: { ...target, ...value } } });
  const changeRequest = (value: Record<string, any>) => changeTarget({ request: { ...request, ...value } });
  const changeResponse = (value: Record<string, any>) => changeTarget({ response: { ...response, ...value } });
  const select = (value: string, options: [string, string][], onSelect: (v: string) => void) =>
    <Select value={value} onChange={onSelect} options={options.map(([v, label]) => ({ value: v, label }))} />;
  if (config.version !== 2) return <Alert type="info" showIcon message="当前使用兼容配置，原有高级设置会完整保留。"
    action={<Button onClick={() => Modal.confirm({ title: '升级此映射的配置', content: '升级后默认遇到缺失字段或类型转换失败时停止同步。源与目标协议参数将分开配置，请检查升级后的请求预览。',
      onOk: () => onChange({ ...config, version: 2, mapping_options: { missing_source: 'error', conversion_error: 'error' },
        protocol: { source: { ...config.protocol }, target: {} } }) })}>升级配置</Button>} />;
  return <Tabs items={[
    { key: 'fields', label: '字段取值', children: <Row gutter={12}>
      <Col span={8}><Form.Item label="HAP 字段键解析">{select(config.protocol?.source?.field_keys || 'raw', [['raw','原样'],['alias','别名'],['id','控件 ID'],['alias_or_id','兼容别名与 ID']], v => onChange({ ...config, protocol: { ...config.protocol, source: { ...config.protocol?.source, field_keys: v } } }))}</Form.Item></Col>
      <Col span={8}><Form.Item label="源字段缺失">{select(config.mapping_options?.missing_source || 'error', [['error','报错停止'],['null','填入 null']], v => onChange({ ...config, mapping_options: { ...config.mapping_options, missing_source: v } }))}</Form.Item></Col>
      <Col span={8}><Form.Item label="转换失败">{select(config.mapping_options?.conversion_error || 'error', [['error','报错停止'],['keep','保留原值']], v => onChange({ ...config, mapping_options: { ...config.mapping_options, conversion_error: v } }))}</Form.Item></Col>
    </Row> },
    ...(isRest ? [{ key: 'request', label: '请求配置', children: <>
      <Row gutter={12}><Col span={8}><Form.Item label="HTTP 方法">{select(target.method || 'POST', ['POST','PUT','PATCH','DELETE'].map(v => [v,v]), v => changeTarget({ method:v }))}</Form.Item></Col>
        <Col span={8}><Form.Item label="请求体格式">{select(request.mode || 'raw', [['raw','原样'],['array','记录数组'],['object','单条对象'],['wrapped_array','对象包裹数组']], v => changeRequest({ mode:v, records_path:v === 'wrapped_array' ? '/data' : undefined, extra_body:{} }))}</Form.Item></Col>
        <Col span={8}><Form.Item label="单次记录上限"><InputNumber min={1} value={request.max_records} onChange={v => changeRequest({ max_records:v })} /></Form.Item></Col></Row>
      {request.mode === 'wrapped_array' && <><Form.Item label="记录数组路径（JSON Pointer）"><Input value={request.records_path} placeholder="/data" onChange={e => changeRequest({ records_path:e.target.value })} /></Form.Item>
        <JsonInput label="外层固定字段" value={request.extra_body || {}} onChange={v => changeRequest({ extra_body:v })} onValid={v => valid('extra',v)} /></>}
      <Form.Item label="无记录时">{select(request.on_empty || 'skip', [['skip','跳过发送'],['error','报错']], v => changeRequest({ on_empty:v }))}</Form.Item>
      <Form.Item label="增量重试幂等请求头（可选）" extra="留空时增量目标重试次数强制为 0；填写时目标接口需真正支持同批请求去重。">
        <Input value={target.idempotency_header || ''} placeholder="例如 Idempotency-Key"
          onChange={e => changeTarget({ idempotency_header: e.target.value || null })} />
      </Form.Item>
      {!target.idempotency_header && <Alert type="info" showIcon style={{ marginBottom: 12 }}
        message="无幂等请求头：增量重试次数为 0，每批仅尝试一次。失败或超时后停止后续批次，水位不变，需人工核对目标结果；不支持重发该失败批次。" />}
      <Form.Item><Checkbox checked={!!target.batch_atomic} onChange={e => changeTarget({ batch_atomic: e.target.checked })}>
        目标接口支持整批原子写入（整批成功或整批失败）
      </Checkbox></Form.Item>
    </> }] : []),
    { key:'validation', label:'数据校验', children:<>
      <Alert type="info" message="规则按目标字段填写，支持 required、type、enum、长度、范围与正则。" />
      <Button style={{ margin:'12px 0' }} onClick={() => onChange({ ...config, validations: Object.fromEntries((config.target_contract || []).map((f: any) => [f.name, { required:!!f.required, type:f.type }])) })}>从目标契约导入规则</Button>
      <JsonInput label="字段校验规则" value={config.validations || {}} onChange={v => onChange({ ...config, validations:v })} onValid={v => valid('validation',v)} />
      <JsonInput label="目标字段契约" value={config.target_contract || []} onChange={v => onChange({ ...config, target_contract:v })} onValid={v => valid('contract',v)} />
    </> },
    ...(isRest ? [{ key:'response', label:'响应判断', children:<>
      <Space style={{ marginBottom:12 }}><Button onClick={() => changeResponse({ success_mode:'http_and_body', body_format:'json', match:'all', conditions:[{path:'/success',operator:'eq',value:true}], message_path:'/msg' })}>使用 success / msg 预设</Button></Space>
      <Form.Item label="成功判断">{select(response.success_mode || 'http_only', [['http_only','仅 HTTP（不验证业务）'],['http_and_body','HTTP 与业务结果均成功']], v => changeResponse({ success_mode:v }))}</Form.Item>
      <Form.Item label="响应格式">{select(response.body_format || 'json', [['json','JSON'],['text','文本']], v => changeResponse({ body_format:v }))}</Form.Item>
      <JsonInput label="接受的 HTTP 状态码（null 表示全部 2xx）" value={response.http_statuses ?? null} onChange={v => changeResponse({ http_statuses:v })} onValid={v => valid('http',v)} />
      {response.success_mode === 'http_and_body' && <><Form.Item label="条件组合">{select(response.match || 'all', [['all','全部满足'],['any','任一满足']], v => changeResponse({ match:v }))}</Form.Item>
        <JsonInput label="成功条件（path / operator / value）" value={response.conditions || []} onChange={v => changeResponse({ conditions:v })} onValid={v => valid('conditions',v)} />
        <Form.Item label="错误消息路径"><Input value={response.message_path} placeholder="/msg" onChange={e => changeResponse({ message_path:e.target.value || null })} /></Form.Item></>}
    </> }] : []),
  ]} />;
}
