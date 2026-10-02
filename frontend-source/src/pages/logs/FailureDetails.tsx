import { Alert, Descriptions, Table } from 'antd';
import type { Log } from '../../stores/logStore';

interface FailureIssue {
  row_index?: number;
  source_field?: string;
  target_field?: string;
  target_field_name?: string;
  rule?: string;
}
interface FailureContext {
  task_name?: string;
  batch_number?: number;
  buffer_id?: string;
  exact_row: boolean;
  scope?: 'rows' | 'batch' | 'unavailable';
  total_records: number;
  matched_records?: number;
  page: number;
  page_size: number;
  record_path?: string;
  source_fields?: string[];
  notice?: string;
  records: { row_number: number; source: unknown; source_pointer?: string; issues?: FailureIssue[] }[];
}
interface FailureError {
  code?: string;
  stage?: string;
  exception_type?: string;
  message?: string;
  status_code?: number;
  target_field_name?: string;
  target_field?: string;
  issues?: FailureIssue[];
  issue_count?: number;
  issues_truncated?: boolean;
  frames?: { file: string; line: number; function: string }[];
}

const stageNames: Record<string, string> = {
  source_read:'读取源数据', fetch:'读取源数据', transform:'字段转换', field_resolve:'字段解析',
  validate:'数据校验', request_build:'构造目标请求', target_http:'目标 HTTP 请求',
  target_business:'目标业务响应', push:'写入目标', hap_lookup:'查询目标',
  hap_verify:'回读校验', hap_add:'新增目标记录', hap_edit:'更新目标记录', delete:'删除对账',
  prepare:'执行前检查', dependency:'任务依赖', source_prepare:'准备源批次', source_read_prepare:'准备源批次', incremental_batch:'增量批次',
};
const ruleNames: Record<string, string> = {
  required:'必填', type:'类型', min:'最小值', max:'最大值', min_length:'最小长度',
  max_length:'最大长度', pattern:'格式', enum:'允许值', readback_mismatch:'回读值不一致',
  target_record_missing:'目标记录未找到', identity_or_ack_unconfirmed:'记录身份或写入应答未确认',
};
const valueView = (value: unknown) => <pre style={{ whiteSpace:'pre-wrap', overflowWrap:'anywhere', margin:0 }}>
  {value === null ? 'null（空值）' : JSON.stringify(value)}
</pre>;

const SourceFields = ({ source }: { source: unknown }) => {
  if (!source || typeof source !== 'object' || Array.isArray(source)) return valueView(source);
  return <Table size="small" pagination={false} rowKey="field"
    dataSource={Object.entries(source).map(([field, value]) => ({ field, value }))}
    columns={[
      { title:'源字段', dataIndex:'field', width:190 },
      { title:'执行时源值（已脱敏）', dataIndex:'value', render:valueView },
    ]} />;
};

export default function FailureDetails({ log, onPageChange }: {
  log: Log;
  onPageChange: (page: number) => void;
}) {
  const context = log.response_data?.failure_context as FailureContext | undefined;
  const error = log.response_data?.error_detail as FailureError | undefined;
  if (log.status && log.status !== 'failed') return null;
  if (!context && !error) return null;
  const scope = context?.scope || (context?.exact_row ? 'rows' : context?.records?.length ? 'batch' : 'unavailable');
  const single = context?.exact_row && (context.matched_records ?? context.records.length) === 1;
  return <section style={{ marginBottom:20 }}>
    <h3>报错数据详情</h3>
    <Alert type={scope === 'rows' ? 'error' : 'info'} showIcon
      message={context?.notice || '尚无源数据定位信息，请结合错误阶段和执行摘要排查'} />
    <Descriptions size="small" column={2} style={{ marginTop:12 }}>
      <Descriptions.Item label="任务">{context?.task_name || log.task_id}</Descriptions.Item>
      <Descriptions.Item label="失败阶段">{stageNames[error?.stage || ''] || error?.stage || log.action}</Descriptions.Item>
      <Descriptions.Item label="定位范围">{{rows:'已定位错误行', batch:'整批范围，具体错误行未知', unavailable:'无可读取源数据'}[scope]}</Descriptions.Item>
      <Descriptions.Item label="批次">{context?.batch_number ?? (context?.buffer_id ? '全量批次' : '未分配或历史未记录')}</Descriptions.Item>
      <Descriptions.Item label="错误代码">{error?.code || '-'}</Descriptions.Item>
      <Descriptions.Item label="异常类型">{error?.exception_type || '-'}</Descriptions.Item>
      {error?.status_code != null && <Descriptions.Item label="HTTP 状态">{error.status_code}</Descriptions.Item>}
      <Descriptions.Item label="目标字段">{error?.target_field_name || error?.target_field || '见错误明细'}</Descriptions.Item>
      <Descriptions.Item label="源字段">{context?.source_fields?.join('、') || '未确定'}</Descriptions.Item>
      <Descriptions.Item label="源集合路径">{context?.record_path || '-'}</Descriptions.Item>
      <Descriptions.Item label="批次记录数">{context?.total_records ?? '-'}</Descriptions.Item>
      {error?.message && <Descriptions.Item label="错误摘要" span={2}>{error.message}</Descriptions.Item>}
    </Descriptions>
    {!!error?.issues?.length && <>
      <h4>错误明细{error.issues_truncated ? '（仅展示前 ' + error.issues.length + ' 项，共 ' + error.issue_count + ' 项）' : ''}</h4>
      <Table size="small" rowKey="issue_key" dataSource={error.issues.map((issue, index) => ({...issue, issue_key:index}))} pagination={{pageSize:10}}
        columns={[
          {title:'批内行号', dataIndex:'row_index', render:v => v == null ? '未定位' : v + 1, width:100},
          {title:'源字段', dataIndex:'source_field', render:v => v || '-'},
          {title:'目标字段', render:(_, issue) => issue.target_field_name || issue.target_field || '-'},
          {title:'规则 / 原因', dataIndex:'rule', render:v => ruleNames[v] || v || '-'},
        ]} />
    </>}
    {context && (single ? context.records.map(record =>
      <div key={record.row_number}><h4>批内第 {record.row_number} 条{record.source_pointer ? ' · ' + record.source_pointer : ''}</h4>
        <SourceFields source={record.source} /></div>
    ) : <Table size="small" rowKey="row_number" dataSource={context.records}
      columns={[
        {title:'批内序号', dataIndex:'row_number', width:95},
        {title:'源记录预览（展开查看全部字段）', dataIndex:'source', ellipsis:true, render:source => JSON.stringify(source)},
      ]}
      expandable={{expandedRowRender:record => <SourceFields source={record.source} />}}
      pagination={{current:context.page, pageSize:context.page_size,
        total:context.matched_records ?? context.total_records, showSizeChanger:false,
        onChange:onPageChange, showTotal:total => (scope === 'rows' ? '已定位 ' : '批次共 ') + total + ' 条'}} />)}
    {!!error?.frames?.length && <details style={{marginTop:16}}><summary>代码位置（不含局部变量和凭据）</summary>
      <pre style={{whiteSpace:'pre-wrap'}}>{error.frames.map(f => f.file + ':' + f.line + ' · ' + f.function).join('\n')}</pre>
    </details>}
  </section>;
}
