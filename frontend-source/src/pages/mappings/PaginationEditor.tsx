import { Alert, Col, Form, Input, InputNumber, Row, Select } from 'antd';
import { JsonInput } from './ContractEditor';

export default function PaginationEditor({ read, onChange, onValid }: {
  read: Record<string, any>; onChange: (read: Record<string, any>) => void; onValid: (valid: boolean) => void;
}) {
  const p = read.pagination || { mode: 'none' };
  const update = (patch: Record<string, any>) => onChange({ ...read, pagination: { ...p, ...patch } });
  return <>
    <Form.Item label="源分页模式">
      <Select aria-label="源分页模式" value={p.mode || 'none'}
        options={[{ value: 'none', label: '不分页（一次返回全部）' }, { value: 'page', label: '页码分页' }, { value: 'cursor', label: '游标分页' }]}
        onChange={mode => onChange({ ...read, pagination: mode === 'none' ? { mode } : {
          mode, location: 'query', page_size: 1000, size_param: 'pageSize', max_pages: 1000,
          ...(mode === 'page' ? { page_param: 'page', page_start: 1, end_condition: 'short_page' } :
            { cursor_param: 'cursor', cursor_start: null, next_cursor_path: '/nextCursor' }),
        } })} />
    </Form.Item>
    {p.mode && p.mode !== 'none' && <>
      <Alert type="info" showIcon style={{ marginBottom: 12 }}
        message="源每页条数与目标每批推送条数独立。分页接口需提供稳定排序或稳定游标；分页参数填写在这里，勿在固定参数中重复填写。" />
      <Row gutter={12}>
        <Col span={8}><Form.Item label="分页参数位置">
          <Select aria-label="分页参数位置" value={p.location || 'query'}
            options={[{ value: 'query', label: 'URL 查询参数' }, { value: 'body', label: 'JSON 请求体顶层字段', disabled: read.method !== 'POST' }]}
            onChange={location => update({ location })} />
        </Form.Item></Col>
        <Col span={8}><Form.Item label="源每页条数"><InputNumber aria-label="源每页条数" min={1} max={100000}
          value={p.page_size ?? 1000} onChange={page_size => update({ page_size })} /></Form.Item></Col>
        <Col span={8}><Form.Item label="最大读取页数"><InputNumber aria-label="最大读取页数" min={1} max={100000}
          value={p.max_pages ?? 1000} onChange={max_pages => update({ max_pages })} /></Form.Item></Col>
        <Col span={12}><Form.Item label={p.mode === 'page' ? '页码参数名' : '游标参数名'} required>
          <Input aria-label={p.mode === 'page' ? '页码参数名' : '游标参数名'}
            value={p.mode === 'page' ? p.page_param ?? 'page' : p.cursor_param ?? 'cursor'}
            onChange={e => update({ [p.mode === 'page' ? 'page_param' : 'cursor_param']: e.target.value })} />
        </Form.Item></Col>
        <Col span={12}><Form.Item label="每页条数参数名" extra="接口不接受此参数时留空；仍需设置实际每页条数。">
          <Input aria-label="每页条数参数名" value={p.size_param === undefined ? 'pageSize' : p.size_param || ''}
            onChange={e => update({ size_param: e.target.value || null })} />
        </Form.Item></Col>
      </Row>
      {p.mode === 'page' ? <>
        <Row gutter={12}>
          <Col span={8}><Form.Item label="起始页码"><InputNumber aria-label="起始页码" min={0} value={p.page_start ?? 1}
            onChange={page_start => update({ page_start })} /></Form.Item></Col>
          <Col span={16}><Form.Item label="分页结束条件">
            <Select aria-label="分页结束条件" value={p.end_condition || 'short_page'}
              options={[{ value: 'short_page', label: '本页条数不足每页条数' }, { value: 'total', label: '累计达到总记录数' },
                { value: 'has_more', label: '是否有下一页为 false' }]}
              onChange={end_condition => update({ end_condition,
                ...(end_condition === 'total' && !p.total_path ? { total_path: '/total' } : {}),
                ...(end_condition === 'has_more' && !p.has_more_path ? { has_more_path: '/hasMore' } : {}),
              })} />
          </Form.Item></Col>
        </Row>
        {p.end_condition === 'total' && <Form.Item label="总记录数响应路径" required extra="JSON Pointer，例如 /data/total；返回值须为非负整数。">
          <Input aria-label="总记录数响应路径" value={p.total_path || ''} onChange={e => update({ total_path: e.target.value })} />
        </Form.Item>}
        {p.end_condition === 'has_more' && <Form.Item label="是否有下一页响应路径" required extra="JSON Pointer，例如 /data/hasMore；返回值须为布尔值。">
          <Input aria-label="是否有下一页响应路径" value={p.has_more_path || ''} onChange={e => update({ has_more_path: e.target.value })} />
        </Form.Item>}
      </> : <>
        <Form.Item label="下一游标响应路径" required extra="JSON Pointer；返回 null 或空字符串结束，字段缺失会报错。">
          <Input aria-label="下一游标响应路径" value={p.next_cursor_path || ''} onChange={e => update({ next_cursor_path: e.target.value })} />
        </Form.Item>
        <JsonInput label="起始游标（JSON 字符串、整数或 null）" value={p.cursor_start ?? null}
          onChange={cursor_start => update({ cursor_start })} onValid={onValid} />
      </>}
    </>}
  </>;
}
