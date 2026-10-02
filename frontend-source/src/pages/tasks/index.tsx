import React, { useEffect, useState } from 'react';
import {
  Table, Button, Modal, Form, Input, Select, InputNumber, Switch,
  Space, Popconfirm, message, Tag, Empty, Descriptions, Alert, Tooltip,
} from 'antd';
import {
  PlusOutlined, ReloadOutlined, PlayCircleOutlined, PauseCircleOutlined, CaretRightOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { useTaskStore, Task } from '../../stores/taskStore';
import { useMappingStore } from '../../stores/mappingStore';
import apiClient from '../../api/client';
import { refreshSourceDisabledReason } from './refreshSource';

const TasksPage: React.FC = () => {
  const { tasks, loading, fetchTasks, createTask, deleteTask, runTask, pauseTask, resumeTask } = useTaskStore();
  const { mappings, fetchMappings } = useMappingStore();

  const [modalOpen, setModalOpen] = useState(false);
  const [running, setRunning] = useState<Record<string, boolean>>({});
  const [form] = Form.useForm();
  const [roundTask, setRoundTask] = useState<Task | null>(null);
  const [roundDetail, setRoundDetail] = useState<any>(null);
  const [roundBusy, setRoundBusy] = useState(false);
  const [hapTask, setHapTask] = useState<Task | null>(null);
  const [hapProgress, setHapProgress] = useState<any>(null);
  const [hapBusy, setHapBusy] = useState(false);
  const loadHap = async (task: Task) => {
    setHapTask(task); setHapBusy(true);
    try { const {data} = await apiClient.get(`/tasks/${task.id}/hap-progress`); setHapProgress(data); }
    finally { setHapBusy(false); }
  };
  const [watermarkTask, setWatermarkTask] = useState<Task | null>(null);
  const [watermarkInfo, setWatermarkInfo] = useState<any>(null);
  const [watermarkValue, setWatermarkValue] = useState('');
  const [clearWatermark, setClearWatermark] = useState(false);
  const [watermarkBusy, setWatermarkBusy] = useState(false);
  const openWatermark = async (task: Task) => {
    setWatermarkBusy(true);
    try {
      const { data } = await apiClient.get(`/tasks/${task.id}/watermark`);
      setWatermarkInfo(data); setWatermarkValue(data.value ?? ''); setClearWatermark(false); setWatermarkTask(task);
    } finally { setWatermarkBusy(false); }
  };
  const confirmWatermark = () => {
    if (!watermarkTask || !watermarkInfo) return;
    const next = clearWatermark ? null : watermarkValue.trim();
    if (next === '') { message.warning('请输入水位，或明确选择清空水位'); return; }
    const taskId = watermarkTask.id;
    const info = watermarkInfo;
    Modal.confirm({
      title: '再次确认：修改增量水位', okText: '确认修改水位', cancelText: '返回检查', okType: 'danger',
      content: <div>
        <p>任务：{info.mapping_name}</p><p>增量字段：{info.field}</p>
        <p>原水位：{info.value ?? '未设置'}</p><p>新水位：{next ?? '清空（重新读取历史数据）'}</p>
        <Alert type="warning" showIcon message="调高水位将跳过尚未同步的历史数据；调低或清空水位可能重新处理已有记录。下次只读取严格大于水位的数据，本操作不会立即执行同步。" />
      </div>,
      onOk: async () => {
        setWatermarkBusy(true);
        try {
          await apiClient.put(`/tasks/${taskId}/watermark`, {value:next, expected_value:info.value,
            expected_version:info.version, config_token:info.config_token, confirmed:true});
          message.success('水位已更新，下次执行时生效'); setWatermarkTask(null); setWatermarkInfo(null);
          await fetchTasks();
        } finally { setWatermarkBusy(false); }
      },
    });
  };
  const selectedMapping = Form.useWatch('mapping_id', form);
  const hapUpsert = (mappings.find(m => m.id === selectedMapping)?.mapping_config as any)?.hap_write_mode === 'upsert';
  const incremental = mappings.find(m => m.id === selectedMapping)?.sync_mode === 'incremental';
  const loadRound = async (task: Task) => {
    setRoundTask(task); setRoundBusy(true);
    try { const { data } = await apiClient.get(`/tasks/${task.id}/round`); setRoundDetail(data); }
    finally { setRoundBusy(false); }
  };

  useEffect(() => {
    fetchTasks();
    fetchMappings();
  }, [fetchTasks, fetchMappings]);

  const handleCreate = async () => {
    const values = await form.validateFields();
    await createTask(values);
    message.success('任务创建成功');
    setModalOpen(false);
    form.resetFields();
  };

  const handleRun = async (id: string) => {
    setRunning((prev) => ({ ...prev, [id]: true }));
    try {
      const result = await runTask(id) as any;
      if (result.status === 'completed' || result.status === 'success') message.success('任务执行完成');
      else if (['failed', 'blocked', 'validation_failed'].includes(result.status)) message.error(result.error || '任务未完成，水位未推进');
      else message.info(result.round_id ? '轮次已受理，可查看批次进度；尚未提交新水位' : '任务已受理，请查看后续执行结果');
      await fetchTasks();
    } catch {
      // error handled by interceptor
    } finally {
      setRunning((prev) => ({ ...prev, [id]: false }));
    }
  };

  const statusPill = (status: string) => {
    const variantMap: Record<string, string> = {
      active: 'success',
      paused: 'warning',
      error: 'danger',
    };
    const variant = variantMap[status] || 'default';
    const labelMap: Record<string, string> = {
      active: '运行中',
      paused: '已暂停',
      error: '错误',
    };
    return (
      <span className={`status-pill status-pill--${variant}`}>
        {labelMap[status] || status}
      </span>
    );
  };

  const mappingMap = Object.fromEntries(mappings.map((m) => [m.id, m.name]));

  const columns: ColumnsType<Task> = [
    {
      title: '映射', dataIndex: 'mapping_id', width: 140,
      render: (id) => <span style={{ color: 'var(--nb-text)' }}>{mappingMap[id] || id}</span>,
    },
    { title: '任务类型', dataIndex: 'task_type', width: 100, render: (v) => <Tag>{v}</Tag> },
    {
      title: '同步模式', dataIndex: 'sync_mode', width: 100,
      render: (v) => <Tag color={v === 'sync' ? 'blue' : v === 'async' ? 'orange' : 'purple'}>{v}</Tag>,
    },
    { title: '状态', dataIndex: 'status', width: 90, render: (v: string) => statusPill(v) },
    { title: '已提交水位', dataIndex: 'last_sync_value', render: (v) => v ?? '未设置' },
    {
      title: 'Cron', dataIndex: 'schedule_cron', width: 130,
      render: (v) => v || <span style={{ color: 'var(--nb-text-secondary)' }}>-</span>,
    },
    {
      title: '阈值', dataIndex: 'auto_threshold', width: 80,
    },
    {
      title: '操作', key: 'actions', width: 280,
      render: (_, record) => (
        <Space size={0} wrap>
          {mappings.find(m => m.id === record.mapping_id)?.sync_mode === 'incremental' &&
            <Button size="small" type="link" disabled={!!record.active_round_id} loading={watermarkBusy && watermarkTask?.id === record.id}
              title={record.active_round_id ? '请先处理未完成轮次' : '手动设置下次同步的起始水位'}
              onClick={() => openWatermark(record)}>设置水位</Button>}
          {(mappings.find(m => m.id === record.mapping_id)?.mapping_config as any)?.hap_write_mode === 'upsert' &&
            <Button size="small" type="link" onClick={() => loadHap(record)}>HAP 写入进度</Button>}
          {mappings.find(m => m.id === record.mapping_id)?.sync_mode === 'incremental' &&
            <Button size="small" type="link" onClick={() => loadRound(record)}>增量进度</Button>}
          <Button
            size="small" type="link"
            icon={<PlayCircleOutlined />}
            loading={running[record.id]}
            onClick={() => handleRun(record.id)}
            style={{ color: 'var(--nb-text)' }}
          >
            运行
          </Button>
          {record.status === 'active' && (
            <Button
              size="small" type="link"
              icon={<PauseCircleOutlined />}
              onClick={() => pauseTask(record.id)}
              style={{ color: 'var(--nb-text-secondary)' }}
            >
              暂停
            </Button>
          )}
          {record.status === 'paused' && (
            <Button
              size="small" type="link"
              icon={<CaretRightOutlined />}
              onClick={() => resumeTask(record.id)}
              style={{ color: 'var(--nb-text-secondary)' }}
            >
              恢复
            </Button>
          )}
          <Popconfirm title="确认删除?" onConfirm={() => deleteTask(record.id)}>
            <Button size="small" type="link" danger>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div style={{ padding: '32px 40px', maxWidth: 1100 }}>
      <h1 className="page-heading">同步任务</h1>

      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        marginBottom: 20, marginTop: 8,
      }}>
        <span style={{ color: 'var(--nb-text-secondary)', fontSize: 13 }}>
          {tasks.length} 个任务
        </span>
        <Space>
          <Button
            icon={<ReloadOutlined />}
            onClick={() => fetchTasks()}
            style={{
              border: '1px solid var(--nb-border)',
              color: 'var(--nb-text-secondary)',
              background: '#fff',
            }}
          >
            刷新
          </Button>
          <Button
            icon={<PlusOutlined />}
            onClick={() => setModalOpen(true)}
            style={{
              background: 'var(--nb-primary)',
              borderColor: 'var(--nb-primary)',
              color: '#fff',
            }}
          >
            新建任务
          </Button>
        </Space>
      </div>

      {!loading && tasks.length === 0 ? (
        <div className="nb-empty-state" style={{
          border: '1px solid var(--nb-border)',
          borderRadius: 8,
          background: '#fff',
          padding: '64px 0',
        }}>
          <Empty
            description={<span style={{ color: 'var(--nb-text-secondary)' }}>暂无同步任务，点击右上角新建</span>}
            image={Empty.PRESENTED_IMAGE_SIMPLE}
          />
        </div>
      ) : (
        <div style={{
          border: '1px solid var(--nb-border)',
          borderRadius: 8,
          overflow: 'hidden',
          background: '#fff',
        }}>
          <Table<Task>
            rowKey="id"
            columns={columns}
            dataSource={tasks}
            loading={loading}
            pagination={tasks.length > 10 ? { pageSize: 10, style: { marginRight: 16 } } : false}
            showHeader={tasks.length > 0}
            locale={{ emptyText: ' ' }}
            components={{
              header: {
                cell: (props: React.ThHTMLAttributes<HTMLTableCellElement>) => (
                  <th
                    {...props}
                    style={{
                      ...props.style,
                      textTransform: 'uppercase',
                      fontSize: 11,
                      letterSpacing: '0.05em',
                      color: 'var(--nb-text-secondary)',
                      fontWeight: 500,
                      borderBottom: '1px solid var(--nb-border)',
                      padding: '10px 16px',
                      background: '#fafafa',
                    }}
                  />
                ),
              },
            }}
          />
        </div>
      )}

      <Modal
        title="新建任务"
        open={modalOpen}
        onOk={handleCreate}
        onCancel={() => { setModalOpen(false); form.resetFields(); }}
        width={520}
        destroyOnClose
      >
        <Form form={form} layout="vertical" initialValues={{ task_type: 'scheduled', sync_mode: 'async', fallback_to_async: false, auto_threshold: 100 }}>
          <Form.Item name="mapping_id" label="字段映射" rules={[{ required: true }]}>
            <Select options={mappings.map((m) => ({ label: m.name, value: m.id }))} showSearch />
          </Form.Item>
          <Form.Item name="task_type" label="任务类型">
            <Select options={[{ label: '定时', value: 'scheduled' }, { label: '实时', value: 'realtime' }]} />
          </Form.Item>
          <Form.Item name="schedule_cron" label="Cron 表达式">
            <Input placeholder="*/5 * * * *" />
          </Form.Item>
          <Form.Item name="sync_mode" label="同步模式">
            <Select options={[
              { label: '同步', value: 'sync' }, { label: '异步', value: 'async' }, { label: '自动', value: 'auto', disabled: incremental || hapUpsert },
            ]} />
          </Form.Item>
          <Form.Item name="fallback_to_async" label="失败转异步" valuePropName="checked">
            <Switch disabled={incremental || hapUpsert} />
          </Form.Item>
          <Form.Item name="auto_threshold" label="自动模式阈值">
            <InputNumber min={1} max={100000} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>
      <Modal title="设置增量水位" open={!!watermarkTask} onCancel={() => !watermarkBusy && setWatermarkTask(null)}
        onOk={confirmWatermark} okText="下一步：确认修改" confirmLoading={watermarkBusy}>
        <Descriptions column={1}>
          <Descriptions.Item label="任务">{watermarkInfo?.mapping_name}</Descriptions.Item>
          <Descriptions.Item label="增量字段">{watermarkInfo?.field}</Descriptions.Item>
          <Descriptions.Item label="水位类型">{watermarkInfo?.type}</Descriptions.Item>
          <Descriptions.Item label="当前水位">{watermarkInfo?.value ?? '未设置'}</Descriptions.Item>
        </Descriptions>
        <Form layout="vertical"><Form.Item label="新水位" extra={watermarkInfo?.api_time_compatible ? '支持毫秒、Z、+08:00 等偏移；无时区按北京时间解释。' : undefined}>
          <Input aria-label="新水位" disabled={clearWatermark} value={watermarkValue} onChange={e => setWatermarkValue(e.target.value)}
            placeholder={watermarkInfo?.type === 'number' ? '例如 10000' : watermarkInfo?.type === 'date' ? '2026-09-14' : watermarkInfo?.type === 'datetime_tz' ? '2026-09-14T00:00:00+08:00' : '2026-09-14 00:00:00'} />
        </Form.Item><Form.Item label="清空水位（重新读取历史数据）">
          <Switch checked={clearWatermark} onChange={setClearWatermark} />
        </Form.Item></Form>
        <Alert type="info" message="修改影响本任务的下次读取范围；不会补发被跳过的数据，也不会取消未完成轮次。" />
      </Modal>
      <Modal title="HAP 写入进度" open={!!hapTask} onCancel={() => {setHapTask(null);setHapProgress(null);}} width={900}
        footer={<Space><Button onClick={() => hapTask && loadHap(hapTask)}>刷新</Button>
          <Popconfirm title="仅取消从未发送的全量批次，保留数据和日志；已发送请求不能取消。" onConfirm={async () => {
            if (!hapTask) return;
            await apiClient.post(`/tasks/${hapTask.id}/hap-cancel-unsent`); await loadHap(hapTask);
          }}><Button disabled={hapTask?.status !== 'paused'}>取消未发送批次（先暂停）</Button></Popconfirm>
          <Button loading={hapBusy} disabled={hapTask?.status !== 'active'} onClick={async () => {
            if (!hapTask) return; setHapBusy(true);
            try { const {data} = await apiClient.post(`/tasks/${hapTask.id}/hap-retry`); if(data.error) message.warning(data.error); await loadHap(hapTask); }
            finally { setHapBusy(false); }
          }}>恢复原计划 / 回查未知结果</Button></Space>}>
        <Alert type="info" showIcon message="已确认记录不会重发。未知请求只回查；目标尚未出现记录不代表新增失败。增量水位在全部批次确认后提交。" />
        <Table rowKey="id" loading={hapBusy} dataSource={hapProgress?.plans || []} size="small"
          columns={[{title:'状态',dataIndex:'status'},{title:'新增',dataIndex:'added'},{title:'更新',dataIndex:'updated'},
            {title:'无变化',dataIndex:'unchanged'},{title:'删除',dataIndex:'deleted'},{title:'待处理',dataIndex:'pending'},{title:'待核对',dataIndex:'unknown'}]}
          expandable={{expandedRowRender:(p:any) => <Table rowKey="index" size="small" dataSource={p.rows} columns={[
            {title:'样本序号',dataIndex:'index'},{title:'操作',dataIndex:'operation'},{title:'状态',dataIndex:'status'}]} />}} />
      </Modal>
      <Modal title="增量轮次进度" open={!!roundTask} onCancel={() => { setRoundTask(null); setRoundDetail(null); }} width={850}
        footer={<Space>{roundTask?.status === 'active' ? <Button loading={roundBusy} onClick={async () => { if (!roundTask) return; setRoundBusy(true); try { await pauseTask(roundTask.id); await loadRound({...roundTask, status:'paused'}); message.success('任务已暂停'); } finally { setRoundBusy(false); } }}>暂停任务</Button> : <Button loading={roundBusy} disabled={!roundTask || roundTask.status !== 'paused'} onClick={async () => { if (!roundTask) return; setRoundBusy(true); try { await resumeTask(roundTask.id); await loadRound({...roundTask, status:'active'}); message.success('任务已恢复'); } finally { setRoundBusy(false); } }}>恢复任务</Button>}<Button loading={roundBusy} onClick={() => roundTask && loadRound(roundTask)}>刷新进度</Button>
          <Popconfirm title="删除失败且未封存的空轮次？这不会删除源/目标数据，但会释放轮次占用。" okText="删除轮次" cancelText="保留" onConfirm={async () => { if (!roundTask) return; setRoundBusy(true); try { await apiClient.delete(`/tasks/${roundTask.id}/round`); message.success('失败轮次已删除'); await fetchTasks(); setRoundTask(null); setRoundDetail(null); } catch (e:any) { message.error(e.response?.data?.detail?.message || '删除失败'); } finally { setRoundBusy(false); } }}>
            <Button disabled={!['blocked', 'retry_wait'].includes(roundDetail?.status) || roundDetail?.sealed_at || roundDetail?.batch_count || roundTask?.status !== 'paused'} loading={roundBusy}>删除失败轮次</Button>
          </Popconfirm>
          <Button disabled={!['blocked', 'retry_wait'].includes(roundDetail?.status) || roundTask?.status !== 'active' || roundDetail?.retry_allowed === false} loading={roundBusy}
            onClick={async () => { if (!roundTask) return; setRoundBusy(true); try { await apiClient.post(`/tasks/${roundTask.id}/round/retry`); await loadRound(roundTask); await fetchTasks(); } catch (e: any) { message.error(e.response?.data?.detail?.message || '重试失败，请刷新进度'); } finally { setRoundBusy(false); } }}>重试未完成批次</Button></Space>}>
        <Alert type="info" message="已提交水位只在整轮全部成功后更新。候选水位表示本轮范围，不代表同步完成。" />
        {roundDetail && <><Descriptions column={2} style={{ marginTop: 12 }}>
          <Descriptions.Item label="增量字段">{mappings.find(m => m.id === roundTask?.mapping_id)?.incremental_field || '-'}</Descriptions.Item>
          <Descriptions.Item label="轮次状态">{roundDetail.status}</Descriptions.Item>
          <Descriptions.Item label="已提交水位">{roundDetail.watermark ?? '无（首次读取）'}</Descriptions.Item>
          <Descriptions.Item label="本轮候选水位">{roundDetail.candidate_watermark ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="成功批次">{roundDetail.completed_batches || 0} / {roundDetail.batch_count || 0}</Descriptions.Item>
          <Descriptions.Item label="自动重试次数（不含首次）">{roundDetail.retry_limit ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="最近成功时间">{tasks.find(t => t.id === roundTask?.id)?.last_run_at || '-'}</Descriptions.Item>
        </Descriptions>
        {roundDetail.error && <Alert type="warning" message={roundDetail.error} />}
        {roundDetail.retry_block_reason && roundDetail.retry_block_reason !== roundDetail.error &&
          <Alert type="warning" message={roundDetail.retry_block_reason} />}
        <Table rowKey="id" size="small" dataSource={roundDetail.batches || []} columns={[
          { title: '批次', dataIndex: 'batch_no', render: (v: number) => v + 1 },
          { title: '状态', dataIndex: 'status' }, { title: '尝试次数', dataIndex: 'attempts' }, { title: '源版本', dataIndex: 'source_revision' }, { title: '错误', dataIndex: 'error' }, { title: '操作', key: 'actions', width: 140, render: (_: unknown, batch: any) => {
            if (!['failed', 'validation_failed'].includes(batch.status)) return null;
            const reason = refreshSourceDisabledReason(roundTask?.status, roundDetail?.status, batch, roundBusy);
            return <Tooltip title={reason}><span><Button size="small" disabled={!!reason} onClick={async () => {
              if (!roundTask) return;
              setRoundBusy(true);
              try {
                await apiClient.post(`/tasks/${roundTask.id}/round/${roundDetail.round_id}/batch/${batch.id}/refresh-source`,
                  null, { params: { expected_revision: batch.source_revision || 0 } });
                message.success('源数据已刷新，请先恢复任务，再重试未完成批次');
                await loadRound(roundTask);
              } catch {
                // 具体错误由请求拦截器展示。
              } finally { setRoundBusy(false); }
            }}>刷新源数据</Button></span></Tooltip>;
          } },
        ]} /></>}
      </Modal>
    </div>
  );
};

export default TasksPage;
