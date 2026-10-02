import React, { useEffect, useState } from 'react';
import {
  Table, Button, Modal, Form, Input, Select, InputNumber, Space, Popconfirm, Tag, message, Card, Alert,
  Row, Col,
} from 'antd';
import { PlusOutlined, ReloadOutlined, ApiOutlined, PlusCircleOutlined, MinusCircleOutlined, BugOutlined, EditOutlined, TableOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { useConnectionStore, Connection } from '../../stores/connectionStore';
import FieldDictDrawer from '../../components/FieldDictDrawer';
import apiClient from '../../api/client';

const PROTOCOL_OPTIONS = [
  { label: 'REST', value: 'rest' },
  { label: 'GraphQL', value: 'graphql' },
  { label: 'SOAP', value: 'soap' },
  { label: 'WebSocket', value: 'websocket' },
  { label: 'gRPC', value: 'grpc' },
  { label: 'Dubbo', value: 'dubbo' },
  { label: '明道云 HAP（工作表 V2）', value: 'hap' },
  { label: 'MySQL', value: 'mysql' },
  { label: 'Oracle', value: 'oracle' },
  { label: 'SQLite', value: 'sqlite' },
  { label: 'PostgreSQL', value: 'postgresql' },
];

const AUTH_OPTIONS = [
  { label: '无认证', value: 'none' },
  { label: 'API Key', value: 'api_key' },
  { label: 'Bearer Token', value: 'bearer' },
  { label: 'Basic Auth', value: 'basic' },
  { label: 'OAuth2', value: 'oauth2' },
  { label: 'Sign 签名', value: 'sign' },
  { label: '自定义 Header', value: 'custom' },
];

// 数据库协议：连接信息存 db_config（host/port/sid|database/username/password），不用 base_url + API 认证
const DB_PROTOCOLS = ['oracle', 'mysql', 'postgresql', 'sqlite'];
const DB_DEFAULT_PORT: Record<string, number> = { oracle: 1521, mysql: 3306, postgresql: 5432 };

// 表单扁平字段 _db_* → 后端 db_config 结构；数据库协议同时设置 db_type 并清掉 API 认证字段
function withDbConfig(values: Record<string, any>) {
  if (values._headers_json !== undefined) values.headers = JSON.parse(values._headers_json || '{}');
  delete values._headers_json;
  if (!values.auth_type || values.auth_type === 'none') {
    values.auth_type = null;
    values.auth_config = null;
  }
  if (DB_PROTOCOLS.includes(values.protocol)) {
    values.db_type = values.protocol;
    values.db_config = values.protocol === 'sqlite'
      ? { database: values._db_name || '' }
      : {
          host: values._db_host || '',
          port: values._db_port ? Number(values._db_port) : DB_DEFAULT_PORT[values.protocol],
          ...(values.protocol === 'oracle'
            ? { sid: values._db_name || '' }
            : { database: values._db_name || '' }),
          username: values._db_username || '',
          password: values._db_password || '',
        };
    values.auth_type = undefined;
    values.auth_config = undefined;
  }
  ['_db_host', '_db_port', '_db_name', '_db_username', '_db_password'].forEach((k) => delete values[k]);
  return values;
}

const ConnectionsPage: React.FC = () => {
  const { connections, loading, fetchConnections, createConnection, updateConnection,
          deleteConnection, testConnection } = useConnectionStore();

  const [modalOpen, setModalOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [testing, setTesting] = useState<Record<string, boolean>>({});
  const [form] = Form.useForm();
  // 编辑模式：记录正在编辑的连接 id；null = 新建
  const [editingId, setEditingId] = useState<string | null>(null);
  // task：连接测试运行日志（在表单弹窗内展示）
  const [testLog, setTestLog] = useState<Array<{ time: string; level: string; message: string }>>([]);
  const [testRunning, setTestRunning] = useState(false);
  // 字段定义抽屉：查看连接的字段、格式与定义
  const [fieldsConn, setFieldsConn] = useState<Connection | null>(null);
  const protocolValue = Form.useWatch('protocol', form);
  const authTypeValue = Form.useWatch('auth_type', form);

  useEffect(() => {
    fetchConnections();
  }, [fetchConnections]);

  const handleCreate = async () => {
    try {
      const values = withDbConfig(await form.validateFields());
      setSubmitting(true);

      // custom auth: 将 Form.List 的键值对数组转为 auth_config 对象
      if (values.auth_type === 'custom' && Array.isArray(values._custom_headers)) {
        const headersObj: Record<string, string> = {};
        (values._custom_headers as Array<{ key?: string; value?: string }>).forEach((item) => {
          if (item.key) {
            headersObj[item.key] = item.value || '';
          }
        });
        values.auth_config = headersObj;
      }
      delete values._custom_headers;

      // 未选认证时清理 auth_config
      if (!values.auth_type) {
        values.auth_config = null;
      }

      if (editingId) {
        await updateConnection(editingId, values);
        message.success('连接已更新');
      } else {
        await createConnection(values);
        message.success('连接创建成功');
      }
      setModalOpen(false);
      setEditingId(null);   // 不复位会让下一次"新建"落进 updateConnection 覆盖旧连接
      form.resetFields();
    } catch (err: any) {
      if (err?.errorFields) {
        // Ant Design 表单校验失败 — 字段错误已自动显示
        return;
      }
      // 只提取可读信息（审查 P2 #31）：FastAPI 422 的 detail 数组带 input 原文
      // （含 auth_config 明文凭据），整段 JSON.stringify 会把密钥弹上屏
      const detail = err?.response?.data?.detail;
      const validation = Array.isArray(detail)
        ? detail.map((item: any) => `${Array.isArray(item.loc) ? item.loc.join('.') : '配置'}：${item.msg || '无效'}`).join('；')
        : undefined;
      const msg = (typeof detail === 'string' ? detail : detail?.message)
        || validation || err?.message || '创建失败';
      message.error(msg);
    } finally {
      setSubmitting(false);
    }
  };

  const handleOpenEdit = (record: Connection) => {
    setEditingId(record.id);
    // auth_config 对象 → 自定义 Header 键值对数组（值可能是脱敏掩码 ***，
    // 保存时保留库中原值的逻辑在提交侧处理）
    const authCfg = record.auth_config || {};
    const customHeaders = Object.entries(authCfg).map(([key, value]) => ({
      key,
      value: String(value ?? ''),
    }));
    // db_config → 表单扁平字段（密码为后端脱敏掩码 ***，保存时后端保留原值）
    const db = record.db_config || {};
    form.setFieldsValue({
      ...record,
      auth_config: authCfg,
      auth_type: record.auth_type || 'none',
      _headers_json: JSON.stringify(record.headers || {}, null, 2),
      _custom_headers: customHeaders.length > 0
        ? customHeaders
        : [{ key: '', value: '' }],
      _db_host: db.host ?? undefined,
      _db_port: db.port ?? undefined,
      _db_name: (db as any).sid || db.database || undefined,
      _db_username: db.username ?? undefined,
      _db_password: db.password ?? '',
    });
    setTestLog([]);
    setModalOpen(true);
  };

  // task（用户反馈）：表单内直接测试，并展示运行日志
  const handleTestConfig = async () => {
    try {
      const values = withDbConfig(await form.validateFields());
      let customHeaders: Array<{ key?: string; value?: string }> = [];
      if (values.auth_type === 'custom' && Array.isArray(values._custom_headers)) {
        customHeaders = values._custom_headers;
        const headersObj: Record<string, string> = {};
        customHeaders.forEach((item) => {
          if (item.key) headersObj[item.key] = item.value || '';
        });
        values.auth_config = headersObj;
      }
      delete values._custom_headers;
      setTestRunning(true);
      setTestLog([{ time: new Date().toLocaleTimeString(), level: 'info',
                    message: '测试请求已发出…' }]);
      // 编辑模式：测已保存配置（掩码值不参与测试）；新建模式：测表单值
      const url = editingId
        ? `/connections/${editingId}/test`
        : '/connections/test';
      const body = editingId ? undefined : values;
      const { data } = await apiClient.post(url, body);
      // 兼容两种返回：/{id}/test 扁平结构 与 /test 的 {result, log} 结构
      const result = data.result ?? data;
      const logLines = data.log || [];
      setTestLog(logLines.length > 0
        ? logLines.map((l: any) => ({
            time: new Date(l.time).toLocaleTimeString(),
            level: l.level,
            message: l.message,
          }))
        : [{
            time: new Date().toLocaleTimeString(),
            level: result.status === 'ok' ? 'success' : 'error',
            message: `status=${result.status}，延迟 ${result.latency_ms}ms` +
                     (result.error ? `，错误: ${result.error}` : ''),
          }]);
      if (result.status === 'ok') {
        message.success(`连接成功，延迟 ${result.latency_ms}ms`, 6);
      } else {
        message.error(`连接失败: ${result.error || result.error_msg || '未知错误'}`, 8);
      }
    } catch (err: any) {
      if (!err?.errorFields) {
        message.error(err?.message || '测试请求失败', 8);
      }
    } finally {
      setTestRunning(false);
    }
  };

  const handleTest = async (id: string) => {
    setTesting((prev) => ({ ...prev, [id]: true }));
    try {
      const result = await testConnection(id);
      if (result.status === 'ok') {
        // 结果 toast 停留 8s：避免"点了没反应"的错觉
        message.success(`连接成功，延迟 ${result.latency_ms}ms`, 8);
      } else {
        message.error(`连接失败: ${result.error}`, 10);
      }
    } finally {
      setTesting((prev) => ({ ...prev, [id]: false }));
    }
  };

  const openFieldsDrawer = (conn: Connection) => setFieldsConn(conn);

  const columns: ColumnsType<Connection> = [
    { title: '名称', dataIndex: 'name', width: 160 },
    { title: '类型', dataIndex: 'connection_type', width: 80, render: (v) => <Tag>{v}</Tag> },
    { title: '协议', dataIndex: 'protocol', width: 100, render: (v) => <Tag color="blue">{v}</Tag> },
    { title: '地址', dataIndex: 'base_url', ellipsis: true },
    {
      title: '认证方式',
      dataIndex: 'auth_type',
      width: 100,
      render: (v) => v ? <Tag color="green">{v}</Tag> : '-',
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 80,
      render: (v) => <span className={v === 'active' ? 'status-pill status-pill--success' : 'status-pill'}>{v}</span>,
    },
    {
      title: '超时(ms)',
      dataIndex: 'timeout_ms',
      width: 100,
    },
    {
      title: '操作',
      key: 'actions',
      width: 300,
      render: (_, record) => (
        <Space>
          <Button
            size="small"
            icon={<EditOutlined />}
            onClick={() => handleOpenEdit(record)}
          >
            编辑
          </Button>
          <Button
            size="small"
            icon={<TableOutlined />}
            onClick={() => openFieldsDrawer(record)}
          >
            字段
          </Button>
          <Button
            size="small"
            icon={<ApiOutlined />}
            loading={testing[record.id]}
            onClick={() => handleTest(record.id)}
          >
            测试
          </Button>
          <Popconfirm title="确认删除此连接?" onConfirm={() => deleteConnection(record.id)}>
            <Button size="small" danger>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
        <h1 className="page-heading" style={{ marginBottom: 0 }}>连接管理</h1>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => fetchConnections()}>刷新</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => {
            // 新建必须从干净态开始：editingId 残留会让保存落进 updateConnection
            setEditingId(null);
            form.resetFields();
            setModalOpen(true);
          }}>
            新建连接
          </Button>
        </Space>
      </div>

      <Card bordered style={{ borderRadius: 'var(--nb-radius-lg)', border: '1px solid var(--nb-border)', boxShadow: 'var(--nb-shadow)' }}>
        {connections.length > 0 ? (
          <Table
            rowKey="id"
            columns={columns}
            dataSource={connections}
            loading={loading}
            pagination={{ pageSize: 10 }}
          />
        ) : (
          <div className="nb-empty-state">
            <div className="nb-empty-state__text">暂无连接</div>
            <div className="nb-empty-state__hint">点击上方「新建连接」按钮创建第一个 API 连接</div>
          </div>
        )}
      </Card>

      <Modal
        title={editingId ? '编辑连接' : '新建连接'}
        open={modalOpen}
        onOk={handleCreate}
        onCancel={() => { setModalOpen(false); form.resetFields(); setEditingId(null); }}
        confirmLoading={submitting}
        width={600}
        destroyOnClose
        okText={editingId ? '保存' : '保存并关闭'}
      >
        <Form form={form} layout="vertical" initialValues={{ connection_type: 'source', timeout_ms: 30000, retry_count: 3 }}>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: '请输入名称' }]}>
            <Input maxLength={100} />
          </Form.Item>
          <Form.Item name="connection_type" label="连接类型">
            <Select options={[{ label: '源端', value: 'source' }, { label: '目标端', value: 'target' }]} />
          </Form.Item>
          <Form.Item name="protocol" label="协议" rules={[{ required: true }]}>
            <Select options={PROTOCOL_OPTIONS} showSearch />
          </Form.Item>

          {protocolValue && DB_PROTOCOLS.includes(protocolValue) ? (
            <>
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 16, borderRadius: 'var(--nb-radius)', border: '1px solid var(--nb-border)' }}
                message="数据库连接信息保存在连接配置中；密码脱敏显示，保持掩码或留空表示不修改原密码。"
              />
              {protocolValue !== 'sqlite' && (
                <Row gutter={12}>
                  <Col span={16}>
                    <Form.Item name="_db_host" label="主机地址" rules={[{ required: true, message: '请输入主机地址' }]}>
                      <Input placeholder="127.0.0.1" />
                    </Form.Item>
                  </Col>
                  <Col span={8}>
                    <Form.Item name="_db_port" label="端口">
                      <InputNumber min={1} max={65535} style={{ width: '100%' }}
                        placeholder={String(DB_DEFAULT_PORT[protocolValue] ?? '')} />
                    </Form.Item>
                  </Col>
                </Row>
              )}
              <Form.Item
                name="_db_name"
                label={protocolValue === 'oracle' ? 'SID / 服务名' : protocolValue === 'sqlite' ? '数据库文件路径' : '数据库名'}
                rules={[{ required: true, message: '请填写' }]}
              >
                <Input placeholder={protocolValue === 'oracle' ? 'XE' : protocolValue === 'sqlite' ? 'E:/data/app.db' : 'dbname'} />
              </Form.Item>
              {protocolValue !== 'sqlite' && (
                <Row gutter={12}>
                  <Col span={12}>
                    <Form.Item name="_db_username" label="用户名" rules={[{ required: true, message: '请输入用户名' }]}>
                      <Input placeholder="system" />
                    </Form.Item>
                  </Col>
                  <Col span={12}>
                    <Form.Item name="_db_password" label="密码">
                      <Input.Password placeholder="***（保持不变）" autoComplete="new-password" />
                    </Form.Item>
                  </Col>
                </Row>
              )}
            </>
          ) : (
            <>
              {protocolValue === 'hap' && (
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 16, borderRadius: 'var(--nb-radius)', border: '1px solid var(--nb-border)' }}
                  message="明道云工作表使用 V2 API。在自定义认证中填写 HAP-Appkey 和 HAP-Sign，由适配器按明道云约定传递。"
                />
              )}

              <Form.Item name="base_url" label="地址">
                <Input placeholder={protocolValue === 'hap' ? 'https://api.mingdao.com' : 'https://api.example.com'} />
              </Form.Item>
              <Form.Item name="auth_type" label="认证方式">
                <Select options={protocolValue === 'hap' ? AUTH_OPTIONS.filter(o => ['none','custom'].includes(o.value)) : AUTH_OPTIONS} allowClear />
              </Form.Item>

              {authTypeValue === 'custom' ? (
                <Form.List name="_custom_headers">
                  {(fields, { add, remove }) => (
                    <div style={{ marginBottom: 16 }}>
                      <div style={{ marginBottom: 8, fontWeight: 500, fontSize: 14, color: 'var(--nb-text)' }}>
                        自定义 Header 配置
                      </div>
                      {fields.map(({ key, name, ...restField }) => (
                        <Space key={key} style={{ display: 'flex', marginBottom: 8 }} align="baseline">
                          <Form.Item {...restField} name={[name, 'key']} style={{ marginBottom: 0 }}>
                            <Input placeholder="Header 名称" style={{ width: 200 }} />
                          </Form.Item>
                          <Form.Item {...restField} name={[name, 'value']} style={{ marginBottom: 0 }}>
                            <Input.Password placeholder="Header 值" style={{ width: 240 }} />
                          </Form.Item>
                          <MinusCircleOutlined
                            style={{ color: 'var(--nb-text-secondary)', cursor: 'pointer' }}
                            onClick={() => remove(name)}
                          />
                        </Space>
                      ))}
                      <Button
                        type="dashed"
                        onClick={() => add()}
                        block
                        icon={<PlusCircleOutlined />}
                        style={{ marginTop: fields.length > 0 ? 4 : 0 }}
                      >
                        添加 Header
                      </Button>
                    </div>
                  )}
                </Form.List>
              ) : authTypeValue === 'api_key' ? (
                <>
                  <Form.Item name={['auth_config', 'key_name']} label="密钥字段名">
                    <Input placeholder="X-API-Key" />
                  </Form.Item>
                  <Form.Item name={['auth_config', 'key_value']} label="密钥值">
                    <Input.Password placeholder="密钥内容" />
                  </Form.Item>
                </>
              ) : authTypeValue === 'bearer' ? <Form.Item name={['auth_config','token']} label="Bearer Token"><Input.Password /></Form.Item>
                : authTypeValue === 'basic' ? <><Form.Item name={['auth_config','username']} label="用户名"><Input /></Form.Item><Form.Item name={['auth_config','password']} label="密码"><Input.Password /></Form.Item></>
                : authTypeValue === 'oauth2' ? <><Form.Item name={['auth_config','token_url']} label="Token URL"><Input /></Form.Item><Form.Item name={['auth_config','client_id']} label="Client ID"><Input /></Form.Item><Form.Item name={['auth_config','client_secret']} label="Client Secret"><Input.Password /></Form.Item><Form.Item name={['auth_config','scopes']} label="Scopes"><Input /></Form.Item></>
                : authTypeValue === 'sign' ? <><Form.Item name={['auth_config','algorithm']} label="签名算法"><Select options={['md5','hmac-sha256'].map(v => ({label:v,value:v}))} /></Form.Item><Form.Item name={['auth_config','secret_key']} label="签名密钥"><Input.Password /></Form.Item></>
                : null}
              <Form.Item name="_headers_json" label="普通请求头（JSON）" rules={[{ validator: async (_, value) => {
                if (!value) return;
                try { const parsed = JSON.parse(value); if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw Error(); }
                catch { throw new Error('请输入 JSON 对象'); }
              } }]}>
                <Input.TextArea rows={4} placeholder={'{"Content-Type":"application/json"}'} />
              </Form.Item>
            </>
          )}

          <Form.Item name="timeout_ms" label="超时(ms)">
            <InputNumber min={1000} max={300000} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="retry_count" label="重试次数">
            <InputNumber min={0} max={10} style={{ width: '100%' }} />
          </Form.Item>

          {/* task（用户反馈）：保存前先测试，日志直接可见 */}
          <Button
            icon={<BugOutlined />}
            loading={testRunning}
            onClick={handleTestConfig}
            style={{ marginBottom: 12 }}
          >
            测试连接（不保存）
          </Button>
          {testLog.length > 0 && (
            <div style={{
              background: '#0d1117', color: '#c9d1d9', borderRadius: 6,
              padding: '10px 12px', fontSize: 12, lineHeight: '20px',
              maxHeight: 180, overflow: 'auto', marginBottom: 12,
            }}>
              {testLog.map((l, i) => (
                <div key={i} style={{ color: l.level === 'error' ? '#f85149' : l.level === 'success' ? '#3fb950' : undefined }}>
                  [{l.time}] {l.message}
                </div>
              ))}
            </div>
          )}
        </Form>
      </Modal>

      <FieldDictDrawer conn={fieldsConn} onClose={() => setFieldsConn(null)} />
    </div>
  );
};

export default ConnectionsPage;
