import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Button, Card, Col, Drawer, Empty, Row, Select, Space, Spin, Table, Tag,
} from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { Line, Pie } from '@ant-design/charts';
import { useNavigate } from 'react-router-dom';
import dayjs from 'dayjs';
import apiClient from '../../api/client';

interface MetricsData {
  sync_count: number;
  success_rate: number;
  p50_latency: number;
  p95_latency: number;
  p99_latency: number;
  queue_depth: number;
  task_status_distribution: Record<string, number>;
}

interface TrendItem {
  timestamp: string;
  success: number;
  failed: number;
}

interface TaskMetric {
  task_id: string;
  task_name: string;
  success: number;
  failed: number;
  last_run_at: string | null;
  avg_duration_ms: number | null;
  buffered_batches: number;
}

interface ConnectionMetric {
  connection_id: string;
  connection_name: string;
  side: string;
  success_rate: number;
  avg_latency_ms: number | null;
  failed: number;
}

interface ErrorMetric {
  error_type: string;
  count: number;
  last_seen: string | null;
  samples: string[];
}

interface TaskRun {
  created_at: string | null;
  action: string | null;
  status: string | null;
  duration_ms: number | null;
  error_message: string | null;
}

const PERIODS = [
  { label: '最近 1 小时', value: '1h' },
  { label: '最近 6 小时', value: '6h' },
  { label: '最近 24 小时', value: '24h' },
  { label: '最近 7 天', value: '7d' },
];

const PERIOD_HOURS: Record<string, number> = { '1h': 1, '6h': 6, '24h': 24, '7d': 24 * 7 };

/** 按周期换算 ISO 时间区间（供跳转日志页复现过滤条件） */
function periodRange(period: string): [string, string] {
  const hours = PERIOD_HOURS[period] ?? 24;
  const to = dayjs();
  return [to.subtract(hours, 'hour').toISOString(), to.toISOString()];
}

const DashboardPage: React.FC = () => {
  const navigate = useNavigate();
  const [metrics, setMetrics] = useState<MetricsData | null>(null);
  const [trend, setTrend] = useState<TrendItem[]>([]);
  const [taskMetrics, setTaskMetrics] = useState<TaskMetric[]>([]);
  const [connMetrics, setConnMetrics] = useState<ConnectionMetric[]>([]);
  const [errors, setErrors] = useState<ErrorMetric[]>([]);
  const [loading, setLoading] = useState(true);
  const [period, setPeriod] = useState('24h');
  // 筛选器（作用于对应区块，避免误以为全局 KPI 也被过滤）
  const [taskFilter, setTaskFilter] = useState<string | undefined>();
  const [connFilter, setConnFilter] = useState<string | undefined>();
  // 下钻抽屉
  const [drillTask, setDrillTask] = useState<TaskMetric | null>(null);
  const [runs, setRuns] = useState<TaskRun[]>([]);
  const [runsLoading, setRunsLoading] = useState(false);

  // 请求序号守卫（审查 P2 #32）：切周期/轮询重叠时，晚到的旧响应不得覆盖新状态
  const fetchSeq = useRef(0);
  const fetchData = useCallback(async () => {
    const seq = ++fetchSeq.current;
    setLoading(true);
    try {
      const [overview, trendData, tasks, conns, errs] = await Promise.allSettled([
        apiClient.get('/metrics/overview', { params: { period } }),
        apiClient.get('/metrics/trend', { params: { period, interval: 1 } }),
        apiClient.get('/metrics/tasks', { params: { period } }),
        apiClient.get('/metrics/connections', { params: { period } }),
        apiClient.get('/metrics/errors', { params: { period, limit: 10 } }),
      ]);
      if (seq !== fetchSeq.current) return;   // 已有更新一次的请求在途：丢弃本轮
      // Each panel updates independently: a new health endpoint must not hide the existing overview.
      if (overview.status === 'fulfilled') setMetrics(overview.value.data);
      if (trendData.status === 'fulfilled') setTrend(trendData.value.data.items || []);
      if (tasks.status === 'fulfilled') setTaskMetrics(
        [...(tasks.value.data || [])].sort((a, b) => b.failed - a.failed || b.success - a.success));
      if (conns.status === 'fulfilled') setConnMetrics(
        [...(conns.value.data || [])].sort((a, b) => a.success_rate - b.success_rate));
      if (errs.status === 'fulfilled') setErrors(errs.value.data || []);
    } catch { /* 拦截器已提示 */ }
    finally { if (seq === fetchSeq.current) setLoading(false); }
  }, [period]);

  useEffect(() => { fetchData(); }, [fetchData]);
  useEffect(() => {
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') fetchData();
    }, 30000);
    // 恢复可见立即刷新（审查 P3：隐藏期不空转，回来时数据即刻追平）
    const onVis = () => { if (document.visibilityState === 'visible') fetchData(); };
    document.addEventListener('visibilitychange', onVis);
    return () => { clearInterval(timer); document.removeEventListener('visibilitychange', onVis); };
  }, [fetchData]);

  const drillSeq = useRef(0);
  const openDrill = async (task: TaskMetric) => {
    const seq = ++drillSeq.current;
    setDrillTask(task);
    setRunsLoading(true);
    try {
      const { data } = await apiClient.get(`/metrics/tasks/${task.task_id}/runs`,
        { params: { limit: 20 } });
      if (seq === drillSeq.current) setRuns(data || []);   // 只接受最后一次下钻
    } catch { if (seq === drillSeq.current) setRuns([]); }
    finally { if (seq === drillSeq.current) setRunsLoading(false); }
  };

  /** 跳转日志页并携带可复现的过滤条件（状态 + 时间范围） */
  const jumpToLogs = (opts: { taskId?: string }) => {
    const [start, end] = periodRange(period);
    const params = new URLSearchParams({
      status: 'failed',
      start_time: start,
      end_time: end,
    });
    if (opts.taskId) params.set('task_id', opts.taskId);
    navigate(`/logs?${params.toString()}`);
  };

  const trendData = trend.map((item) => ({
    time: item.timestamp?.substring(5, 16) || '',
    value: item.success,
    type: '成功',
  }));

  const pieData = metrics
    ? Object.entries(metrics.task_status_distribution).map(([status, count]) => ({
      type: status, value: count,
    }))
    : [];

  const trendConfig = {
    data: trendData,
    xField: 'time',
    yField: 'value',
    color: ['#2383E2'],
    smooth: true,
    lineStyle: { lineWidth: 2 },
    point: { size: 2, shape: 'circle' },
    areaStyle: { fill: 'l(270) 0:rgba(35,131,226,0.12) 1:rgba(35,131,226,0)' },
    xAxis: { grid: null, line: { style: { stroke: '#E9E9E7' } } },
    yAxis: { grid: { line: { style: { stroke: '#F1F0EF', lineWidth: 1 } } } },
  };

  const pieTotal = pieData.reduce((sum, item) => sum + item.value, 0);

  const pieConfig = {
    data: pieData,
    angleField: 'value',
    colorField: 'type',
    radius: 0.75,
    innerRadius: 0.6,
    color: ['#2383E2', '#5BA0F2', '#E9E9E7', '#0F7B4B', '#D9730D', '#E03E3E'],
    // G2 v5 标签用 text 函数；v1 的 '{name}' 模板串会被表达式编译器
    // 误当表达式解析并抛错，导致整张饼图渲染失败
    label: {
      position: 'outside',
      text: (d: { type?: string; value?: number }) => {
        const pct = pieTotal ? ((d.value ?? 0) / pieTotal) * 100 : 0;
        return `${d.type ?? ''}\n${pct.toFixed(1)}%`;
      },
    },
    statistic: {
      title: { content: '任务总数', style: { fontSize: 12, color: '#9B9A97' } },
      content: {
        style: { fontSize: 22, fontWeight: 700, color: '#37352F' },
        content: pieData.reduce((sum, item) => sum + item.value, 0).toString(),
      },
    },
  };

  const visibleTasks = taskFilter
    ? taskMetrics.filter((t) => t.task_id === taskFilter)
    : taskMetrics;
  const visibleConns = connFilter
    ? connMetrics.filter((c) => c.connection_id === connFilter)
    : connMetrics;

  return (
    <div>
      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        marginBottom: 24, flexWrap: 'wrap', gap: 12,
      }}>
        <h1 className="page-heading" style={{ marginBottom: 0 }}>监控看板</h1>
        <Space wrap>
          <Select
            value={period}
            onChange={setPeriod}
            size="small"
            style={{ width: 150 }}
            options={PERIODS}
          />
          <Select
            allowClear
            size="small"
            placeholder="按任务筛选"
            style={{ width: 200 }}
            value={taskFilter}
            onChange={setTaskFilter}
            options={taskMetrics.map((t) => ({
              label: `${t.task_name}${t.failed ? `（失败 ${t.failed}）` : ''}`,
              value: t.task_id,
            }))}
          />
          <Select
            allowClear
            size="small"
            placeholder="按连接筛选"
            style={{ width: 190 }}
            value={connFilter}
            onChange={setConnFilter}
            options={connMetrics.map((c) => ({
              label: `${c.connection_name}（${c.side === 'source' ? '源' : '目标'}）`,
              value: c.connection_id,
            }))}
          />
          <Button size="small" icon={<ReloadOutlined />} onClick={fetchData}>刷新</Button>
        </Space>
      </div>

      <Spin spinning={loading}>
        {/* KPI Cards */}
        <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
          <Col xs={12} sm={6}>
            <div className="metric-card">
              <div className="metric-card__label">同步总量</div>
              <div className="metric-card__value">{metrics?.sync_count ?? '-'}</div>
            </div>
          </Col>
          <Col xs={12} sm={6}>
            <div className="metric-card">
              <div className="metric-card__label">成功率</div>
              <div className="metric-card__value">
                {metrics ? `${metrics.success_rate.toFixed(1)}` : '-'}
                <span className="metric-card__suffix">%</span>
              </div>
            </div>
          </Col>
          <Col xs={12} sm={6}>
            <div className="metric-card">
              <div className="metric-card__label">队列深度</div>
              <div className="metric-card__value">{metrics?.queue_depth ?? '-'}</div>
            </div>
          </Col>
          <Col xs={12} sm={6}>
            <div className="metric-card">
              <div className="metric-card__label">P95 延迟</div>
              <div className="metric-card__value">
                {metrics ? Math.round(metrics.p95_latency) : '-'}
                <span className="metric-card__suffix">ms</span>
              </div>
            </div>
          </Col>
        </Row>

        {/* Charts */}
        <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
          <Col xs={24} lg={16}>
            <Card title="同步趋势" size="small">
              {trendData.length > 0 ? (
                <Line {...trendConfig} height={260} />
              ) : (
                <div className="nb-empty-state">
                  <div className="nb-empty-state__text">暂无趋势数据</div>
                  <div className="nb-empty-state__hint">同步数据将在此展示</div>
                </div>
              )}
            </Card>
          </Col>
          <Col xs={24} lg={8}>
            <Card title="任务分布" size="small">
              {pieData.length > 0 ? (
                <Pie {...pieConfig} height={260} />
              ) : (
                <div className="nb-empty-state">
                  <div className="nb-empty-state__text">暂无数据</div>
                </div>
              )}
            </Card>
          </Col>
        </Row>

        {/* 任务健康度 */}
        <Card
          title="任务健康度"
          size="small"
          style={{ marginBottom: 24 }}
          extra={<span style={{ fontSize: 12, color: 'var(--nb-text-secondary)' }}>
            点击行可下钻最近执行明细
          </span>}
        >
          <Table<TaskMetric>
            size="small"
            rowKey="task_id"
            dataSource={visibleTasks}
            pagination={{ pageSize: 8, hideOnSinglePage: true }}
            onRow={(record) => ({ onClick: () => openDrill(record), style: { cursor: 'pointer' } })}
            locale={{ emptyText: <Empty description="暂无任务数据" /> }}
            columns={[
              { title: '任务', dataIndex: 'task_name', ellipsis: true },
              { title: '成功日志', dataIndex: 'success', width: 80 },
              {
                title: '失败日志', dataIndex: 'failed', width: 80,
                render: (v: number) => (v > 0
                  ? <Tag color="red">{v}</Tag>
                  : <span style={{ color: 'var(--nb-text-muted)' }}>0</span>),
              },
              { title: '缓冲批次数', dataIndex: 'buffered_batches', width: 100 },
              {
                title: '平均耗时', dataIndex: 'avg_duration_ms', width: 110,
                render: (v: number | null) => (v == null ? '-' : `${Math.round(v)} ms`),
              },
              {
                title: '最后运行', dataIndex: 'last_run_at', width: 170,
                render: (v: string | null) => (v
                  ? dayjs(v).format('MM-DD HH:mm:ss')
                  : <span style={{ color: 'var(--nb-text-muted)' }}>从未运行</span>),
              },
            ]}
            rowClassName={(record) => (record.failed > 0 ? 'row-has-failure' : '')}
          />
        </Card>

        <Row gutter={[16, 16]}>
          {/* 连接健康度 */}
          <Col xs={24} lg={12}>
            <Card title="连接健康度（关联任务日志）" size="small">
              <Table<ConnectionMetric>
                size="small"
                rowKey={(r) => `${r.connection_id}-${r.side}`}
                dataSource={visibleConns}
                pagination={{ pageSize: 6, hideOnSinglePage: true }}
                locale={{ emptyText: <Empty description="暂无连接调用数据" /> }}
                columns={[
                  { title: '连接', dataIndex: 'connection_name', ellipsis: true },
                  {
                    title: '方向', dataIndex: 'side', width: 70,
                    render: (v: string) => <Tag>{v === 'source' ? '源' : '目标'}</Tag>,
                  },
                  {
                    title: '成功率', dataIndex: 'success_rate', width: 90,
                    render: (v: number) => (
                      <span style={{ color: v < 95 ? 'var(--nb-danger)' : 'var(--nb-text)' }}>
                        {v.toFixed(1)}%
                      </span>
                    ),
                  },
                  { title: '失败日志', dataIndex: 'failed', width: 70 },
                  {
                    title: '平均延迟', dataIndex: 'avg_latency_ms', width: 100,
                    render: (v: number | null) => (v == null ? '-' : `${Math.round(v)} ms`),
                  },
                ]}
              />
            </Card>
          </Col>

          {/* 错误 Top N */}
          <Col xs={24} lg={12}>
            <Card title="错误 Top N" size="small">
              {errors.length === 0 ? (
                <Empty description="窗口内没有失败记录" />
              ) : (
                <Table<ErrorMetric>
                  size="small"
                  rowKey="error_type"
                  dataSource={errors}
                  pagination={false}
                  onRow={() => ({
                    onClick: () => jumpToLogs({}),
                    style: { cursor: 'pointer' },
                  })}
                  columns={[
                    {
                      title: '错误类型', dataIndex: 'error_type', ellipsis: true,
                      render: (v: string, r: ErrorMetric) => (
                        <span title={r.samples.join('\n')}>{v}</span>
                      ),
                    },
                    {
                      title: '次数', dataIndex: 'count', width: 80,
                      render: (v: number) => <Tag color="red">{v}</Tag>,
                    },
                    {
                      title: '最近出现', dataIndex: 'last_seen', width: 150,
                      render: (v: string | null) => (v
                        ? dayjs(v).format('MM-DD HH:mm:ss') : '-'),
                    },
                  ]}
                />
              )}
              <div style={{ marginTop: 8, fontSize: 12, color: 'var(--nb-text-secondary)' }}>
                点击任意行可跳转到执行日志（已带上「失败 + 当前时间范围」过滤条件）
              </div>
            </Card>
          </Col>
        </Row>
      </Spin>

      {/* 下钻抽屉：任务最近执行明细 */}
      <Drawer
        title={drillTask ? `任务执行明细：${drillTask.task_name}` : '任务执行明细'}
        open={Boolean(drillTask)}
        onClose={() => setDrillTask(null)}
        width={720}
        extra={drillTask ? (
          <Button size="small" type="primary" danger
                  onClick={() => jumpToLogs({ taskId: drillTask.task_id })}>
            查看失败日志
          </Button>
        ) : null}
      >
        {drillTask ? (
          <Space direction="vertical" style={{ width: '100%' }} size="middle">
            <Space size="large" wrap>
              <span>成功 <b>{drillTask.success}</b></span>
              <span>失败 <b style={{ color: 'var(--nb-danger)' }}>{drillTask.failed}</b></span>
              <span>缓冲批次数 <b>{drillTask.buffered_batches}</b></span>
              <span>平均耗时 <b>{drillTask.avg_duration_ms == null
                ? '-' : `${Math.round(drillTask.avg_duration_ms)} ms`}</b></span>
            </Space>
            <Table<TaskRun>
              size="small"
              rowKey={(r) => `${r.created_at ?? ''}|${r.action ?? ''}|${r.status ?? ''}`
                + `|${r.duration_ms ?? ''}|${r.error_message ?? ''}`}
              loading={runsLoading}
              dataSource={runs}
              pagination={{ pageSize: 10, hideOnSinglePage: true }}
              locale={{ emptyText: <Empty description="该任务暂无执行记录" /> }}
              columns={[
                {
                  title: '时间', dataIndex: 'created_at', width: 160,
                  render: (v: string | null) => (v
                    ? dayjs(v).format('MM-DD HH:mm:ss') : '-'),
                },
                { title: '动作', dataIndex: 'action', width: 90 },
                {
                  title: '状态', dataIndex: 'status', width: 90,
                  render: (v: string | null) => (
                    <span className={`status-pill ${v === 'success'
                      ? 'status-pill--success' : 'status-pill--danger'}`}>{v}</span>
                  ),
                },
                {
                  title: '耗时(ms)', dataIndex: 'duration_ms', width: 100,
                  render: (v: number | null) => v ?? '-',
                },
                {
                  title: '错误', dataIndex: 'error_message', ellipsis: true,
                  render: (v: string | null) => (v
                    ? <span style={{ color: 'var(--nb-danger)' }}>{v}</span> : '-'),
                },
              ]}
            />
          </Space>
        ) : null}
      </Drawer>
    </div>
  );
};

export default DashboardPage;
