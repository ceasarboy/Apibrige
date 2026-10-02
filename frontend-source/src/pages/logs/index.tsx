import React, { useEffect, useRef, useState } from 'react';
import { Table, Button, Select, Space, DatePicker, Drawer, Descriptions, Spin } from 'antd';
import { ReloadOutlined, EyeOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { useLogStore, Log } from '../../stores/logStore';
import dayjs from 'dayjs';
import { useSearchParams } from 'react-router-dom';
import apiClient from '../../api/client';
import { useTaskStore } from '../../stores/taskStore';
import { useMappingStore } from '../../stores/mappingStore';
import FailureDetails from './FailureDetails';

const { RangePicker } = DatePicker;

const LogsPage: React.FC = () => {
  const [searchParams] = useSearchParams();
  const { logs, total, loading, fetchLogs } = useLogStore();

  const { tasks, fetchTasks } = useTaskStore();
  const { mappings, fetchMappings } = useMappingStore();
  const [taskFilter, setTaskFilter] = useState<string | undefined>();
  const [detailBusy, setDetailBusy] = useState(false);
  const detailRequest = useRef(0);
  useEffect(() => { fetchTasks(); fetchMappings(); }, [fetchTasks, fetchMappings]);

  const [drawerOpen, setDrawerOpen] = useState(false);
  const [selectedLog, setSelectedLog] = useState<Log | null>(null);
  const [statusFilter, setStatusFilter] = useState<string | undefined>();
  const [actionFilter, setActionFilter] = useState<string | undefined>();
  const [timeRange, setTimeRange] = useState<[string, string] | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);

  useEffect(() => {
    setTaskFilter(searchParams.get('task_id') || undefined);
    setStatusFilter(searchParams.get('status') || undefined);
    const start = searchParams.get('start_time'), end = searchParams.get('end_time');
    setTimeRange(start && end && dayjs(start).isValid() && dayjs(end).isValid() ? [start, end] : null);
    setPage(1);
  }, [searchParams]);

  useEffect(() => {
    const params: Record<string, unknown> = { page, page_size: pageSize };
    if (taskFilter) params.task_id = taskFilter;
    if (statusFilter) params.status = statusFilter;
    if (actionFilter) params.action = actionFilter;
    if (timeRange) {
      params.start_time = timeRange[0];
      params.end_time = timeRange[1];
    }
    fetchLogs(params);
  }, [page, pageSize, taskFilter, statusFilter, actionFilter, timeRange, fetchLogs]);

  const showDetail = async (log: Log, recordPage = 1) => {
    const request = ++detailRequest.current;
    setSelectedLog(log); setDrawerOpen(true); setDetailBusy(true);
    try {
      const { data } = await apiClient.get('/logs/' + log.id, { params: { record_page: recordPage } });
      if (request === detailRequest.current) setSelectedLog(data);
    } catch {
      // 请求错误由统一拦截器提示，保留已有日志摘要。
    } finally {
      if (request === detailRequest.current) setDetailBusy(false);
    }
  };

  const columns: ColumnsType<Log> = [
    { title: '时间', dataIndex: 'created_at', width: 180, render: (v) => dayjs(v).format('YYYY-MM-DD HH:mm:ss') },
    { title: '任务', dataIndex: 'task_id', width: 220, ellipsis: true,
      render: id => mappings.find(m => m.id === tasks.find(t => t.id === id)?.mapping_id)?.name || id },
    {
      title: '动作', dataIndex: 'action', width: 100,
      render: (v) => <span className="status-pill status-pill--info">{v}</span>,
    },
    {
      title: '状态', dataIndex: 'status', width: 90,
      render: (v) => (
        <span className={`status-pill ${v === 'success' ? 'status-pill--success' : 'status-pill--danger'}`}>
          {v}
        </span>
      ),
    },
    { title: '失败原因', dataIndex: 'error_message', width: 260, ellipsis: true, render: v => v || '-' },
    { title: '耗时(ms)', dataIndex: 'duration_ms', width: 100, render: (v) => v ?? '-' },
    {
      title: '操作', key: 'actions', width: 80,
      render: (_, record) => (
        <Button size="small" icon={<EyeOutlined />} onClick={() => showDetail(record)}>详情</Button>
      ),
    },
  ];

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <h2 className="page-heading">执行日志</h2>
        <Button icon={<ReloadOutlined />} onClick={() => fetchLogs({ page, page_size: pageSize, task_id: taskFilter, status: statusFilter, action: actionFilter, start_time: timeRange?.[0], end_time: timeRange?.[1] })}>刷新</Button>
      </div>

      <Space wrap style={{ marginBottom: 16 }}>
        <Select placeholder="按任务过滤" allowClear showSearch optionFilterProp="label"
          style={{ width: 300 }} value={taskFilter} onChange={v => { setTaskFilter(v); setPage(1); }}
          options={tasks.map(t => ({ value: t.id, label: mappings.find(m => m.id === t.mapping_id)?.name || t.id }))} />
        <Select
          placeholder="按状态过滤"
          allowClear
          style={{ width: 130 }}
          value={statusFilter}
          onChange={(v) => { setStatusFilter(v); setPage(1); }}
          options={[{ label: '成功', value: 'success' }, { label: '失败', value: 'failed' }, {label:'跳过',value:'skipped'}]}
        />
        <Select
          placeholder="按动作过滤"
          allowClear
          style={{ width: 130 }}
          value={actionFilter}
          onChange={(v) => { setActionFilter(v); setPage(1); }}
          options={[
            { label: '增量批次', value: 'incremental_batch' }, { label: '回读校验', value: 'hap_verify' },
            { label: '源读取', value: 'source_read' }, { label: '拉取', value: 'fetch' }, { label: '转换', value: 'transform' }, { label: '推送', value: 'push' },
            { label:'字段解析', value:'field_resolve' }, {label:'数据校验',value:'validate'},
            { label:'请求构造', value:'request_build' }, {label:'目标HTTP',value:'target_http'}, {label:'业务响应',value:'target_business'},
          ]}
        />
        <RangePicker
          showTime
          onChange={(dates) => {
            if (dates?.[0] && dates?.[1]) {
              setTimeRange([dates[0].toISOString(), dates[1].toISOString()]);
            } else {
              setTimeRange(null);
            }
            setPage(1);
          }}
        />
      </Space>

      <Table
        rowKey="id"
        columns={columns}
        dataSource={logs}
        loading={loading}
        pagination={{
          current: page,
          pageSize,
          total,
          onChange: (p, ps) => { setPage(p); setPageSize(ps); },
          showTotal: (t) => `共 ${t} 条`,
        }}
      />

      <Drawer
        title="日志详情"
        open={drawerOpen}
        onClose={() => { detailRequest.current++; setDetailBusy(false); setDrawerOpen(false); }}
        width={960}
      >
        <Spin spinning={detailBusy}>
        {selectedLog && <FailureDetails log={selectedLog} onPageChange={p => showDetail(selectedLog, p)} />}
        {selectedLog && (
          <Descriptions bordered size="small" column={1}>
            <Descriptions.Item label="ID">{selectedLog.id}</Descriptions.Item>
            <Descriptions.Item label="任务ID">{selectedLog.task_id}</Descriptions.Item>
            <Descriptions.Item label="状态">{selectedLog.status}</Descriptions.Item>
            <Descriptions.Item label="动作">{selectedLog.action}</Descriptions.Item>
            {(selectedLog.response_data as any)?.business_verified === false && <Descriptions.Item label="业务结果">仅 HTTP 成功，未验证业务结果</Descriptions.Item>}
            <Descriptions.Item label="耗时">{selectedLog.duration_ms}ms</Descriptions.Item>
            <Descriptions.Item label="创建时间">{dayjs(selectedLog.created_at).format('YYYY-MM-DD HH:mm:ss')}</Descriptions.Item>
            {selectedLog.error_message && (
              <Descriptions.Item label="错误信息">
                <pre style={{ color: 'red', whiteSpace: 'pre-wrap' }}>{selectedLog.error_message}</pre>
              </Descriptions.Item>
            )}
            {selectedLog.request_data && (
              <Descriptions.Item label="请求数据">
                <pre style={{ whiteSpace: 'pre-wrap', maxHeight: 200, overflow: 'auto' }}>
                  {JSON.stringify(selectedLog.request_data, null, 2)}
                </pre>
              </Descriptions.Item>
            )}
            {selectedLog.response_data && (
              <Descriptions.Item label="响应数据">
                <pre style={{ whiteSpace: 'pre-wrap', maxHeight: 200, overflow: 'auto' }}>
                  {JSON.stringify(selectedLog.response_data, null, 2)}
                </pre>
              </Descriptions.Item>
            )}
          </Descriptions>
        )}
        </Spin>
      </Drawer>
    </div>
  );
};

export default LogsPage;
