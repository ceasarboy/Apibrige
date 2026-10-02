import React, { useEffect, useState } from 'react';
import {
  Card, Tabs, Table, Button, Modal, Form, Input, InputNumber, Select, Space,
  Popconfirm, message, Upload, Row, Col, Statistic, Spin, Descriptions,
} from 'antd';
import {
  PlusOutlined, DeleteOutlined, EditOutlined, ExportOutlined, ImportOutlined,
  SendOutlined, ThunderboltOutlined, SwapOutlined, FileTextOutlined,
  CheckCircleOutlined, CloseCircleOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import apiClient from '../../api/client';

// ============================================================
// Types
// ============================================================

interface TestCaseItem {
  id: string;
  name: string;
  description: string;
  group: string;
  tags: string[];
  source_endpoint: string;
  target_endpoint: string;
  request_data: Record<string, unknown>;
  field_mapping: Record<string, string>;
  key_field: string;
  concurrency: number;
  duration_ms: number;
  ramp_up: boolean;
  created_at: string;
}

interface SendResult {
  total: number;
  success: number;
  failed: number;
  success_rate: number;
  avg_latency_ms: number;
  max_latency_ms: number;
}

interface StressResult {
  total_requests: number;
  successful: number;
  failed: number;
  success_rate: number;
  qps: number;
  avg_latency_ms: number;
  p50_latency_ms: number;
  p95_latency_ms: number;
  p99_latency_ms: number;
  elapsed_ms: number;
}

interface CompareResult {
  total: number;
  matched: number;
  mismatched: number;
  details: CompareDetail[];
}

interface CompareDetail {
  matched: boolean;
  match_rate: number;
  summary: string;
  diffs: { field: string; expected: string; actual: string; reason: string }[];
}

// ============================================================
// Test Case Management
// ============================================================

const CaseTab: React.FC<{
  onSelect?: (c: TestCaseItem) => void;
}> = ({ onSelect }) => {
  const [cases, setCases] = useState<TestCaseItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<TestCaseItem | null>(null);
  const [groupFilter, setGroupFilter] = useState<string | undefined>();
  const [groups, setGroups] = useState<string[]>([]);
  const [form] = Form.useForm();

  const fetchCases = async () => {
    setLoading(true);
    try {
      const params: Record<string, string> = {};
      if (groupFilter) params.group = groupFilter;
      const { data } = await apiClient.get('/testing/cases', { params });
      setCases(data);
    } catch { /* handled */ }
    finally { setLoading(false); }
  };

  const fetchGroups = async () => {
    try {
      const { data } = await apiClient.get('/testing/cases/groups');
      setGroups(data);
    } catch { /* handled */ }
  };

  useEffect(() => { fetchCases(); fetchGroups(); }, [groupFilter]);

  const handleSave = async () => {
    const values = await form.validateFields();
    try {
      if (editing) {
        await apiClient.put(`/testing/cases/${editing.id}`, values);
        message.success('已更新');
      } else {
        await apiClient.post('/testing/cases', values);
        message.success('已创建');
      }
      setModalOpen(false);
      fetchCases();
    } catch { /* handled */ }
  };

  const handleDelete = async (id: string) => {
    await apiClient.delete(`/testing/cases/${id}`);
    message.success('已删除');
    fetchCases();
  };

  const handleExport = async () => {
    const { data } = await apiClient.get('/testing/cases/export');
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'test-cases.json'; a.click();
    URL.revokeObjectURL(url);
    message.success('已导出');
  };

  const handleImport = async (file: File) => {
    const text = await file.text();
    await apiClient.post('/testing/cases/import', text, {
      headers: { 'Content-Type': 'text/plain' },
    });
    message.success('已导入');
    fetchCases();
    return false;
  };

  const columns: ColumnsType<TestCaseItem> = [
    { title: '名称', dataIndex: 'name', width: 160, ellipsis: true },
    { title: '分组', dataIndex: 'group', width: 100 },
    {
      title: '标签', dataIndex: 'tags', width: 200,
      render: (tags: string[]) => tags?.map((t: string) => (
        <span key={t} className="status-pill">{t}</span>
      )),
    },
    { title: '源端点', dataIndex: 'source_endpoint', width: 180, ellipsis: true },
    { title: '目标端点', dataIndex: 'target_endpoint', width: 180, ellipsis: true },
    { title: '并发', dataIndex: 'concurrency', width: 60 },
    { title: '时长(ms)', dataIndex: 'duration_ms', width: 90 },
    { title: '创建时间', dataIndex: 'created_at', width: 140, ellipsis: true },
    {
      title: '操作', width: 220, fixed: 'right',
      render: (_, record) => (
        <Space>
          <Button size="small" icon={<ThunderboltOutlined />}
            onClick={() => onSelect?.(record)}>执行</Button>
          <Button size="small" icon={<EditOutlined />}
            onClick={() => { setEditing(record); form.setFieldsValue(record); setModalOpen(true); }} />
          <Popconfirm title="确认删除?" onConfirm={() => handleDelete(record.id)}>
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <>
      <Space style={{ marginBottom: 16 }}>
        <Button type="primary" icon={<PlusOutlined />}
          onClick={() => { setEditing(null); form.resetFields(); setModalOpen(true); }}>
          新建用例
        </Button>
        <Select allowClear placeholder="按分组筛选" style={{ width: 140 }}
          value={groupFilter} onChange={setGroupFilter}
          options={groups.map((g) => ({ label: g, value: g }))} />
        <Button icon={<ExportOutlined />} onClick={handleExport}>导出</Button>
        <Upload accept=".json" showUploadList={false} beforeUpload={handleImport}>
          <Button icon={<ImportOutlined />}>导入</Button>
        </Upload>
      </Space>

      <Table rowKey="id" columns={columns} dataSource={cases} loading={loading}
        scroll={{ x: 1200 }} size="middle" pagination={{ pageSize: 15 }} />

      {/* Create / Edit Modal */}
      <Modal title={editing ? '编辑用例' : '新建用例'} open={modalOpen}
        onOk={handleSave} onCancel={() => setModalOpen(false)} width={640} destroyOnClose>
        <Form form={form} layout="vertical">
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="name" label="名称" rules={[{ required: true }]}>
                <Input placeholder="例: 用户同步-基础测试" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="group" label="分组">
                <Input placeholder="例: 登录模块" />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} />
          </Form.Item>
          <Form.Item name="tags" label="标签（逗号分隔）"
            getValueFromEvent={(e) => e.target.value.split(',').map((s: string) => s.trim()).filter(Boolean)}>
            <Input placeholder="例: smoke, regression" />
          </Form.Item>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="source_endpoint" label="源端点">
                <Input placeholder="/api/users" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="target_endpoint" label="目标端点">
                <Input placeholder="/api/customers" />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item name="concurrency" label="并发数">
                <InputNumber min={1} max={100} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="duration_ms" label="压测时长(ms)">
                <InputNumber min={1000} max={60000} step={1000} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="ramp_up" label="梯度加压" valuePropName="checked">
                <Select options={[{ label: '是', value: true }, { label: '否', value: false }]} />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>
    </>
  );
};

// ============================================================
// Data Send
// ============================================================

const SendTab: React.FC = () => {
  const [template, setTemplate] = useState('{"name": "user-{{index}}", "email": "{{index}}@test.com"}');
  const [count, setCount] = useState(10);
  const [concurrency, setConcurrency] = useState(2);
  const [result, setResult] = useState<SendResult | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSend = async () => {
    setLoading(true);
    try {
      let templateObj: Record<string, unknown>;
      try { templateObj = JSON.parse(template); } catch { message.error('JSON 格式错误'); setLoading(false); return; }
      const { data } = await apiClient.post('/testing/send', {
        template: templateObj, count, concurrency,
      });
      setResult(data);
      message.success('发送完成');
    } catch { /* handled */ }
    finally { setLoading(false); }
  };

  return (
    <Spin spinning={loading}>
      <Row gutter={[16, 16]}>
        <Col span={12}>
          <Card title="发送配置" style={{ height: '100%' }}>
            <Form layout="vertical">
              <Form.Item label="数据模板 (JSON, 支持 {{index}} {{timestamp}} {{uuid}})">
                <Input.TextArea rows={5} value={template}
                  onChange={(e) => setTemplate(e.target.value)}
                  style={{ fontFamily: 'monospace' }} />
              </Form.Item>
              <Row gutter={16}>
                <Col span={8}>
                  <Form.Item label="发送数量">
                    <InputNumber min={1} max={10000} value={count} onChange={(v) => setCount(v || 1)} />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item label="并发数">
                    <InputNumber min={1} max={50} value={concurrency} onChange={(v) => setConcurrency(v || 1)} />
                  </Form.Item>
                </Col>
                <Col span={8} style={{ display: 'flex', alignItems: 'center', paddingTop: 30 }}>
                  <Button type="primary" icon={<SendOutlined />} onClick={handleSend}>发送</Button>
                </Col>
              </Row>
            </Form>
          </Card>
        </Col>
        <Col span={12}>
          <Card title="发送结果" style={{ height: '100%' }}>
            {result ? (
              <Row gutter={[16, 16]}>
                <Col span={12}>
                  <Statistic title="总数" value={result.total} />
                </Col>
                <Col span={12}>
                  <Statistic title="成功" value={result.success} valueStyle={{ color: '#3f8600' }} />
                </Col>
                <Col span={12}>
                  <Statistic title="成功率" value={result.success_rate * 100} suffix="%" precision={1} />
                </Col>
                <Col span={12}>
                  <Statistic title="平均延迟" value={result.avg_latency_ms} suffix="ms" precision={1} />
                </Col>
                <Col span={12}>
                  <Statistic title="最大延迟" value={result.max_latency_ms} suffix="ms" precision={1} />
                </Col>
              </Row>
            ) : (
              <div className="nb-empty-state">
                <div className="nb-empty-state__text">点击"发送"执行</div>
              </div>
            )}
          </Card>
        </Col>
      </Row>
    </Spin>
  );
};

// ============================================================
// Stress Test
// ============================================================

const StressTab: React.FC = () => {
  const [concurrency, setConcurrency] = useState(10);
  const [duration, setDuration] = useState(5000);
  const [rampUp, setRampUp] = useState(true);
  const [result, setResult] = useState<StressResult | null>(null);
  const [loading, setLoading] = useState(false);

  const handleStress = async () => {
    setLoading(true);
    try {
      const { data } = await apiClient.post('/testing/stress', {
        concurrency, duration_ms: duration, ramp_up: rampUp,
      });
      setResult(data);
      message.success('压测完成');
    } catch { /* handled */ }
    finally { setLoading(false); }
  };

  return (
    <Spin spinning={loading}>
      <Row gutter={[16, 16]}>
        <Col span={8}>
          <Card title="压测配置">
            <Form layout="vertical">
              <Form.Item label="并发数">
                <InputNumber min={1} max={100} value={concurrency}
                  onChange={(v) => setConcurrency(v || 1)} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item label="压测时长 (ms)">
                <InputNumber min={1000} max={60000} step={1000} value={duration}
                  onChange={(v) => setDuration(v || 5000)} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item label="梯度加压">
                <Select value={rampUp} onChange={setRampUp}
                  options={[{ label: '开启', value: true }, { label: '关闭', value: false }]} />
              </Form.Item>
              <Button type="primary" icon={<ThunderboltOutlined />} block onClick={handleStress}>
                开始压测
              </Button>
            </Form>
          </Card>
        </Col>
        <Col span={16}>
          <Card title="压测结果">
            {result ? (
              <Row gutter={[16, 16]}>
                <Col span={8}><Statistic title="总请求" value={result.total_requests} /></Col>
                <Col span={8}><Statistic title="成功" value={result.successful} valueStyle={{ color: '#3f8600' }} /></Col>
                <Col span={8}><Statistic title="失败" value={result.failed} valueStyle={{ color: result.failed > 0 ? '#cf1322' : '' }} /></Col>
                <Col span={8}><Statistic title="成功率" value={(result.success_rate * 100).toFixed(1)} suffix="%" /></Col>
                <Col span={8}><Statistic title="QPS" value={result.qps.toFixed(1)} /></Col>
                <Col span={8}><Statistic title="耗时" value={result.elapsed_ms} suffix="ms" /></Col>
                <Col span={8}><Statistic title="P50" value={result.p50_latency_ms.toFixed(1)} suffix="ms" /></Col>
                <Col span={8}><Statistic title="P95" value={result.p95_latency_ms.toFixed(1)} suffix="ms" /></Col>
                <Col span={8}><Statistic title="P99" value={result.p99_latency_ms.toFixed(1)} suffix="ms" /></Col>
              </Row>
            ) : (
              <div className="nb-empty-state">
                <div className="nb-empty-state__text">点击"开始压测"</div>
              </div>
            )}
          </Card>
        </Col>
      </Row>
    </Spin>
  );
};

// ============================================================
// Data Compare
// ============================================================

const CompareTab: React.FC = () => {
  const [sourceText, setSourceText] = useState('[{"id": 1, "name": "张三"}, {"id": 2, "name": "李四"}]');
  const [targetText, setTargetText] = useState('[{"id": 1, "name": "张三"}, {"id": 2, "name": "王五"}]');
  const [mappingText, setMappingText] = useState('');
  const [keyField, setKeyField] = useState('id');
  const [result, setResult] = useState<CompareResult | null>(null);
  const [loading, setLoading] = useState(false);

  const handleCompare = async () => {
    setLoading(true);
    let sourceData: unknown; let targetData: unknown;
    try { sourceData = JSON.parse(sourceText); } catch { message.error('源数据不是合法 JSON'); setLoading(false); return; }
    try { targetData = JSON.parse(targetText); } catch { message.error('目标数据不是合法 JSON'); setLoading(false); return; }
    try {
      const body: Record<string, unknown> = { source_data: sourceData, target_data: targetData };
      if (keyField) body.key_field = keyField;
      if (mappingText) {
        try { body.field_mapping = JSON.parse(mappingText); } catch { message.error('映射 JSON 格式错误'); setLoading(false); return; }
      }
      const { data } = await apiClient.post('/testing/compare', body);
      setResult(data);
    } catch { /* 拦截器已提示 */ }
    finally { setLoading(false); }
  };

  return (
    <Spin spinning={loading}>
      <Row gutter={[16, 16]}>
        <Col span={12}>
          <Card title="源数据 (JSON)" size="small">
            <Input.TextArea rows={8} value={sourceText}
              onChange={(e) => setSourceText(e.target.value)} style={{ fontFamily: 'monospace' }} />
          </Card>
        </Col>
        <Col span={12}>
          <Card title="目标数据 (JSON)" size="small">
            <Input.TextArea rows={8} value={targetText}
              onChange={(e) => setTargetText(e.target.value)} style={{ fontFamily: 'monospace' }} />
          </Card>
        </Col>
        <Col span={12}>
          <Form layout="inline">
            <Form.Item label="关联字段">
              <Input value={keyField} onChange={(e) => setKeyField(e.target.value)} style={{ width: 120 }} />
            </Form.Item>
            <Form.Item label="字段映射 (JSON)">
              <Input placeholder='{"src":"tgt"}' value={mappingText}
                onChange={(e) => setMappingText(e.target.value)} style={{ width: 200, fontFamily: 'monospace' }} />
            </Form.Item>
            <Form.Item>
              <Button type="primary" icon={<SwapOutlined />} onClick={handleCompare}>比对</Button>
            </Form.Item>
          </Form>
        </Col>
      </Row>

      {result && (
        <Card title="比对结果" style={{ marginTop: 16 }}>
          <Row gutter={16} style={{ marginBottom: 16 }}>
            <Col span={6}><Statistic title="总数" value={result.total} /></Col>
            <Col span={6}><Statistic title="匹配" value={result.matched}
              valueStyle={{ color: '#3f8600' }} prefix={<CheckCircleOutlined />} /></Col>
            <Col span={6}><Statistic title="不匹配" value={result.mismatched}
              valueStyle={{ color: result.mismatched > 0 ? '#cf1322' : '' }} prefix={<CloseCircleOutlined />} /></Col>
            <Col span={6}><Statistic title="匹配率"
              value={(result.total > 0 ? result.matched / result.total * 100 : 0).toFixed(1)} suffix="%" /></Col>
          </Row>
          {result.details.map((d, i) => (
            <Card key={i} size="small" style={{ marginBottom: 8 }}
              title={`记录 ${i + 1} - ${d.matched ? '匹配' : '不匹配'}`}
              extra={<span className={`status-pill ${d.matched ? 'status-pill--success' : 'status-pill--danger'}`}>{d.matched ? 'PASS' : 'FAIL'}</span>}>
              <Descriptions column={4} size="small">
                <Descriptions.Item label="匹配率">{d.match_rate}</Descriptions.Item>
                <Descriptions.Item label="摘要">{d.summary}</Descriptions.Item>
              </Descriptions>
              {d.diffs.length > 0 && (
                <Table rowKey="field" dataSource={d.diffs}
                  columns={[
                    { title: '字段', dataIndex: 'field', width: 150 },
                    { title: '期望', dataIndex: 'expected', width: 200, ellipsis: true },
                    { title: '实际', dataIndex: 'actual', width: 200, ellipsis: true },
                    { title: '原因', dataIndex: 'reason' },
                  ]}
                  pagination={false} size="small" style={{ marginTop: 8 }} />
              )}
            </Card>
          ))}
        </Card>
      )}
    </Spin>
  );
};

// ============================================================
// Report
// ============================================================

const ReportTab: React.FC = () => {
  const [htmlContent, setHtmlContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [title, setTitle] = useState('APIBridge 测试报告');

  const fetchReport = async (format: 'html' | 'json') => {
    setLoading(true);
    try {
      const { data } = await apiClient.get(`/testing/report/${format}`, {
        params: { title },
      });
      if (format === 'html') {
        setHtmlContent(data);
      } else {
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = 'test-report.json'; a.click();
        URL.revokeObjectURL(url);
        message.success('JSON 报告已下载');
      }
    } catch { /* handled */ }
    finally { setLoading(false); }
  };

  return (
    <Spin spinning={loading}>
      <Space style={{ marginBottom: 16 }}>
        <Input value={title} onChange={(e) => setTitle(e.target.value)}
          placeholder="报告标题" style={{ width: 250 }} />
        <Button icon={<FileTextOutlined />} onClick={() => fetchReport('html')}>生成 HTML 报告</Button>
        <Button icon={<ExportOutlined />} onClick={() => fetchReport('json')}>导出 JSON</Button>
      </Space>

      {htmlContent ? (
        <Card>
          {/* sandbox 空值 = 全禁（报告是静态 HTML，不需要脚本/表单/弹窗） */}
          <iframe srcDoc={htmlContent} sandbox="" title="测试报告"
                  style={{ width: '100%', height: 600, border: 'none' }} />
        </Card>
      ) : (
        <Card>
          <div className="nb-empty-state">
            <div className="nb-empty-state__text">点击"生成 HTML 报告"查看</div>
          </div>
        </Card>
      )}
    </Spin>
  );
};

// ============================================================
// Main Testing Page
// ============================================================

const TestingPage: React.FC = () => {
  const tabItems = [
    { key: 'cases', label: '测试用例', children: <CaseTab /> },
    { key: 'send', label: '数据发送', children: <SendTab /> },
    { key: 'stress', label: '压力测试', children: <StressTab /> },
    { key: 'compare', label: '数据比对', children: <CompareTab /> },
    { key: 'report', label: '报告', children: <ReportTab /> },
  ];

  return (
    <div>
      <h2 className="page-heading">收发比对</h2>
      <Tabs items={tabItems} />
    </div>
  );
};

export default TestingPage;
