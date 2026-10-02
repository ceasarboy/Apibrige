import { useEffect, useState } from 'react';
import { Alert, Col, Form, Input, Row, Select } from 'antd';
import { JsonInput } from './ContractEditor';
import PaginationEditor from './PaginationEditor';

export default function SourceReadEditor({ config, onChange, onValid, incrementalMode }: {
  config: Record<string, any>;
  onChange: (config: Record<string, any>) => void;
  onValid: (valid: boolean) => void;
  incrementalMode: boolean;
}) {
  const [validity, setValidity] = useState<Record<string, boolean>>({});
  const source = config.protocol?.source || {};
  const read = source.read || { method: 'GET', records_path: '' };
  const incremental = source.incremental || { key_field: '', field_type: 'datetime' };
  const update = (patch: Record<string, any>) => onChange({ ...config,
    protocol: { ...config.protocol, source: { ...source, read, ...(incrementalMode ? { incremental } : {}), ...patch } } });
  const valid = (key: string, value: boolean) => setValidity(v => v[key] === value ? v : { ...v, [key]: value });
  useEffect(() => {
    onValid((!incrementalMode || (config.version === 2 && !!source.read && !!source.incremental?.key_field?.trim())) && Object.values(validity).every(Boolean));
  }, [config.version, source.read, source.incremental, validity, incrementalMode]);
  useEffect(() => () => onValid(true), []);
  if (config.version !== 2) return <Alert type="info" showIcon
    message="配置 REST 源读取或分页前，请先在下方将映射升级为新版配置。" />;
  return <>
    <Alert type="info" showIcon style={{ marginBottom: 12 }}
      message={incrementalMode ? 'REST 源支持不分页、页码和游标分页。分页增量会比较两次完整读取后再按水位筛选并分批推送；源主键必须唯一，水位不能为空。' :
        'REST 源支持不分页、页码和游标分页。未配置时沿用原有读取；修改下方设置后按指定响应数组路径读取。'} />
    <Row gutter={12}>
      <Col span={8}><Form.Item label="源读取方式">
        <Select aria-label="源读取方式" value={read.method || 'GET'} options={['GET', 'POST'].map(value => ({ value }))}
          onChange={method => update({ read: { ...read, method, body: method === 'GET' ? null : read.body || {},
            ...(method === 'GET' && read.pagination ? { pagination: { ...read.pagination, location: 'query' } } : {}) } })} />
      </Form.Item></Col>
      <Col span={16}><Form.Item label="源记录数组路径" extra="根数组留空；包装数组填写 /data 或 /data/rows。">
        <Input aria-label="源记录数组路径" value={read.records_path ?? ''} placeholder="留空表示响应本身就是数组"
          onChange={e => update({ read: { ...read, records_path: e.target.value } })} />
      </Form.Item></Col>
      {incrementalMode && <><Col span={12}><Form.Item label="源记录主键字段" required extra="填写记录对象的顶层字段名，例如 id。">
        <Input aria-label="源记录主键字段" value={incremental.key_field || ''} placeholder="例如 id"
          onChange={e => update({ incremental: { ...incremental, key_field: e.target.value.trim() } })} />
      </Form.Item></Col>
      <Col span={12}><Form.Item label="源水位类型" required extra="日期时间均兼容带时区及毫秒格式；无时区按北京时间 UTC+08:00 解释。">
        <Select aria-label="源水位类型" value={incremental.field_type || 'datetime'}
          options={[{ value: 'datetime', label: '日期时间（统一北京时间）' }, { value: 'datetime_tz', label: '日期时间（保留时区）' },
            { value: 'date', label: '日期' }, { value: 'number', label: '数字版本号' }]}
          onChange={field_type => update({ incremental: { ...incremental, field_type } })} />
      </Form.Item></Col></>}
    </Row>
    <PaginationEditor read={read} onChange={next => update({ read: next })} onValid={v => valid('pagination', v)} />
    <JsonInput label="源固定查询参数（支持 {{last_watermark}}）" value={read.query || {}}
      onChange={query => update({ read: { ...read, query } })} onValid={v => valid('query', v)} />
    {read.method === 'POST' && <JsonInput label="源 JSON 请求体" value={read.body || {}}
      onChange={body => update({ read: { ...read, body } })} onValid={v => valid('body', v)} />}
    <JsonInput label="源业务成功判断（可选；空对象表示仅检查 HTTP）" value={source.response || {}}
      onChange={response => update({ response })} onValid={v => valid('response', v)} />
  </>;
}
