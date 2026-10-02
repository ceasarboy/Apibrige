import React, { useEffect, useMemo, useState } from 'react';
import {
  Card, Table, Button, Select, Input, Modal, Form, Space, Tag, Tooltip,
  Popconfirm, Typography, message,
} from 'antd';
import {
  PlusOutlined, ReloadOutlined, CloudDownloadOutlined, TableOutlined,
} from '@ant-design/icons';
import apiClient from '../../api/client';
import FieldDictDrawer from '../../components/FieldDictDrawer';

interface BridgeRef {
  mapping_id: string;
  mapping_name: string;
  side: string;
  peer_connection_id: string;
  peer_connection_name: string;
  peer_form: string;
}

interface FormRow {
  id: string;
  connection_id: string;
  connection_name: string;
  form_key: string;
  original_name: string | null;
  display_name: string | null;
  remark: string | null;
  source: string;
  bridge_count: number;
  bridges: BridgeRef[];
  updated_at: string;
}

/** 桥引用明细（Tooltip 内容）：这条表单被哪些桥、以哪个方向用了 */
const BridgeList: React.FC<{ bridges: BridgeRef[] }> = ({ bridges }) => (
  <div style={{ maxWidth: 320 }}>
    {bridges.map((b) => (
      <div key={`${b.mapping_id}-${b.side}`} style={{ marginBottom: 6 }}>
        <div style={{ fontWeight: 600 }}>{b.mapping_name}</div>
        <div style={{ fontSize: 12, opacity: 0.85 }}>
          {b.side === 'source' ? '作为源' : '作为目标'} → {b.peer_connection_name || '对端连接'}
          {' / '}
          <Typography.Text code style={{ fontSize: 11, color: 'inherit' }}>
            {b.peer_form || '-'}
          </Typography.Text>
        </div>
      </div>
    ))}
  </div>
);

const FormsPage: React.FC = () => {
  const [rows, setRows] = useState<FormRow[]>([]);
  const [connections, setConnections] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [connFilter, setConnFilter] = useState<string>();
  const [syncing, setSyncing] = useState(false);

  // 手动登记
  const [createOpen, setCreateOpen] = useState(false);
  const [createForm] = Form.useForm();
  // 编辑
  const [editRow, setEditRow] = useState<FormRow | null>(null);
  const [editForm] = Form.useForm();
  // 字段设置抽屉（与连接管理页共用同一份字段字典）
  const [fieldsRow, setFieldsRow] = useState<FormRow | null>(null);

  const connOptions = useMemo(
    () => connections.map((c) => ({ label: `${c.name} (${c.protocol})`, value: c.id })),
    [connections],
  );

  // 抽屉需要 protocol/db_type 才能决定按表名还是工作表 ID 取字段
  const fieldsConn = useMemo(() => {
    if (!fieldsRow) return null;
    const c = connections.find((x) => x.id === fieldsRow.connection_id);
    return {
      id: fieldsRow.connection_id,
      name: c?.name || fieldsRow.connection_name,
      protocol: c?.protocol || '',
      db_type: c?.db_type || null,
    };
  }, [fieldsRow, connections]);

  const load = async (connId?: string) => {
    setLoading(true);
    try {
      const { data } = await apiClient.get('/forms', {
        params: connId ? { connection_id: connId } : undefined,
      });
      setRows(data);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    apiClient.get('/connections').then(({ data }) => setConnections(data));
  }, []);

  useEffect(() => { load(connFilter); }, [connFilter]);

  // 从源端拉取：DB 列表名 / HAP 列工作表；REST 等无结构协议后端返回 400
  const syncForms = async () => {
    if (!connFilter) {
      message.warning('请先在上方选择一个连接，再拉取它的表单清单');
      return;
    }
    setSyncing(true);
    try {
      const { data } = await apiClient.post(`/forms/${connFilter}/sync`);
      message.success(`拉取完成：新增 ${data.added}，刷新 ${data.refreshed}，共 ${data.total}`);
      await load(connFilter);
    } catch {
      // 错误提示由 axios 拦截器统一给出（400=不支持自动拉取，502=拉取失败）
    } finally {
      setSyncing(false);
    }
  };

  const submitCreate = async () => {
    const v = await createForm.validateFields();
    await apiClient.post('/forms', v);
    message.success('表单已登记');
    setCreateOpen(false);
    createForm.resetFields();
    load(connFilter);
  };

  const submitEdit = async () => {
    if (!editRow) return;
    const v = await editForm.validateFields();
    await apiClient.put(`/forms/${editRow.id}`, v);
    message.success('已保存');
    setEditRow(null);
    load(connFilter);
  };

  const removeForm = async (id: string) => {
    await apiClient.delete(`/forms/${id}`);
    message.success('表单注册记录已删除（已建映射不受影响）');
    load(connFilter);
  };

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
        <h1 className="page-heading" style={{ marginBottom: 0 }}>表单注册表</h1>
        <Space>
          <Select
            allowClear showSearch optionFilterProp="label"
            style={{ width: 240 }}
            placeholder="按连接筛选（默认全部）"
            value={connFilter}
            onChange={setConnFilter}
            options={connOptions}
          />
          <Button icon={<CloudDownloadOutlined />} loading={syncing} onClick={syncForms}>
            从源端拉取
          </Button>
          <Button icon={<ReloadOutlined />} onClick={() => load(connFilter)}>刷新</Button>
          <Button type="primary" icon={<PlusOutlined />}
            onClick={() => {
              createForm.resetFields();
              if (connFilter) createForm.setFieldsValue({ connection_id: connFilter });
              setCreateOpen(true);
            }}>
            手动登记
          </Button>
        </Space>
      </div>

      <Card bordered style={{ borderRadius: 'var(--nb-radius-lg)', border: '1px solid var(--nb-border)', boxShadow: 'var(--nb-shadow)' }}>
        <Table
          rowKey="id"
          dataSource={rows}
          loading={loading}
          pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 个表单` }}
          columns={[
            {
              title: '表单名称',
              width: 220,
              render: (_, r: FormRow) => (
                <div>
                  <div style={{ fontWeight: 600 }}>{r.display_name || r.form_key}</div>
                  {r.original_name && r.original_name !== r.display_name && (
                    <div style={{ fontSize: 12, color: 'var(--nb-text-muted)' }}>
                      源端名：{r.original_name}
                    </div>
                  )}
                </div>
              ),
            },
            {
              title: '技术标识',
              dataIndex: 'form_key',
              width: 240,
              render: (v: string) => (
                <Typography.Text code copyable={{ text: v }} style={{ fontSize: 12 }}>
                  {v}
                </Typography.Text>
              ),
            },
            {
              title: '所属连接',
              dataIndex: 'connection_name',
              width: 160,
              render: (v: string) => v || '-',
            },
            {
              title: '来源',
              dataIndex: 'source',
              width: 90,
              render: (v: string) =>
                v === 'auto' ? <Tag color="blue">自动拉取</Tag> : <Tag>手动登记</Tag>,
            },
            {
              title: '已建桥',
              width: 100,
              sorter: (a: FormRow, b: FormRow) => a.bridge_count - b.bridge_count,
              render: (_, r: FormRow) =>
                r.bridge_count > 0 ? (
                  <Tooltip title={<BridgeList bridges={r.bridges} />}>
                    <Tag color="green" style={{ cursor: 'pointer' }}>
                      {r.bridge_count} 条桥
                    </Tag>
                  </Tooltip>
                ) : (
                  <Tag style={{ color: 'var(--nb-text-muted)' }}>未建桥</Tag>
                ),
            },
            {
              title: '备注',
              dataIndex: 'remark',
              ellipsis: true,
              render: (v: string | null) => v || '-',
            },
            {
              title: '更新时间',
              dataIndex: 'updated_at',
              width: 170,
              render: (v: string) => (v ? new Date(v).toLocaleString('zh-CN') : '-'),
            },
            {
              title: '操作',
              width: 220,
              render: (_, r: FormRow) => (
                <Space>
                  <Button size="small" icon={<TableOutlined />} onClick={() => setFieldsRow(r)}>
                    字段
                  </Button>
                  <Button size="small" onClick={() => {
                    setEditRow(r);
                    editForm.setFieldsValue({
                      display_name: r.display_name || '', remark: r.remark || '',
                    });
                  }}>
                    编辑
                  </Button>
                  <Popconfirm
                    title="删除表单注册记录？"
                    description={r.bridge_count > 0
                      ? `该表单被 ${r.bridge_count} 条桥引用，删除登记不会断开这些桥。`
                      : '仅清理登记信息，不影响映射与任务。'}
                    onConfirm={() => removeForm(r.id)}
                  >
                    <Button size="small" danger>删除</Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Modal
        title="手动登记表单"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={submitCreate}
        okText="登记"
        destroyOnClose
      >
        <Form form={createForm} layout="vertical">
          <Form.Item name="connection_id" label="所属连接"
            rules={[{ required: true, message: '请选择连接' }]}>
            <Select showSearch optionFilterProp="label"
              placeholder="选择连接" options={connOptions} />
          </Form.Item>
          <Form.Item name="form_key" label="技术标识"
            tooltip="HAP 填 worksheet_id；数据库填表名；REST 等填自定义标识（如接口路径）"
            rules={[{ required: true, message: '请填写表单的技术标识' }]}>
            <Input placeholder="如 6aa32f0cf46d7b7148ac6f29 / MTL_MATERIAL / /material/listPage"
              maxLength={200} />
          </Form.Item>
          <Form.Item name="display_name" label="显示名"
            tooltip="留空则与技术标识相同">
            <Input placeholder="如 MES 物料列表" maxLength={200} />
          </Form.Item>
          <Form.Item name="remark" label="备注">
            <Input.TextArea rows={2} placeholder="这个表单是什么、给谁用的" maxLength={500} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`编辑表单${editRow ? `：${editRow.form_key}` : ''}`}
        open={!!editRow}
        onCancel={() => setEditRow(null)}
        onOk={submitEdit}
        okText="保存"
        destroyOnClose
      >
        <Form form={editForm} layout="vertical">
          <Form.Item name="display_name" label="显示名">
            <Input maxLength={200} />
          </Form.Item>
          <Form.Item name="remark" label="备注">
            <Input.TextArea rows={2} maxLength={500} />
          </Form.Item>
          <div style={{ fontSize: 12, color: 'var(--nb-text-muted)' }}>
            技术标识与来源不可修改；源端名会在下次拉取时自动刷新。
          </div>
        </Form>
      </Modal>

      <FieldDictDrawer
        conn={fieldsConn}
        formKey={fieldsRow?.form_key}
        formLabel={fieldsRow && fieldsRow.display_name && fieldsRow.display_name !== fieldsRow.form_key
          ? fieldsRow.display_name : undefined}
        onClose={() => setFieldsRow(null)}
      />
    </div>
  );
};

export default FormsPage;
