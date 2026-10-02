import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert, Button, Card, Form, Input, Modal, Popconfirm, Select, Space, Switch,
  Table, Tag, Tooltip, Typography, message,
} from 'antd';
import {
  CopyOutlined, DeleteOutlined, PlusOutlined, ReloadOutlined, SyncOutlined,
} from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import apiClient from '../../api/client';
import { useBoards } from './BiBoard';

/**
 * 访问授权（add-bi-access-scope）：只读令牌 + 可见范围（看板 / 行过滤 / 列隐藏）
 *
 * 设计要点（服务端强制，本页只做配置）：
 * - 令牌明文**只在创建/轮换时出现一次**，库内只存哈希；
 * - 行过滤字段必须是卡片维度之一，否则该卡对这份授权不可见（宁可少给，不可多漏）；
 * - 投放链接把令牌放在 `?token=`，投放页进来自动写入 localStorage 并抹掉地址栏参数。
 */

interface Profile {
  key: string;
  name: string;
  enabled: boolean;
  boards: string[];
  row_filters: Record<string, string>;
  hidden_fields: string[];
  note?: string | null;
  token_prefix: string;
  last_used_at?: string | null;
  created_at?: string | null;
}

/** "字段:值" 多行文本 ↔ {字段: 值}（行过滤的轻量录入形态） */
function parseKv(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  text.split('\n').map((l) => l.trim()).filter(Boolean).forEach((line) => {
    const idx = line.indexOf(':');
    if (idx > 0) out[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
  });
  return out;
}

const fmtKv = (obj: Record<string, string>): string =>
  Object.entries(obj || {}).map(([k, v]) => `${k}:${v}`).join('\n');

const BiAccessPage: React.FC = () => {
  const navigate = useNavigate();
  const [rows, setRows] = useState<Profile[]>([]);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<Profile | null>(null);
  // 一次性令牌按档案 key 绑定：全局单值会让 B 行的"复制投放链接"拼上 A 行的令牌
  const [tokenShown, setTokenShown] = useState<{ key: string; token: string } | null>(null);
  const [form] = Form.useForm();
  const { boards } = useBoards();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await apiClient.get('/bi/access/profiles');
      setRows(data ?? []);
    } catch {
      message.error('读取授权档案失败（需要管理令牌）');
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { load(); }, [load]);

  const openCreate = () => {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue({ enabled: true, boards: [], row_filters_text: '', hidden_text: '' });
    setOpen(true);
  };

  const openEdit = (row: Profile) => {
    setEditing(row);
    form.resetFields();       // 清残留：条件回填字段（note 等）不能带上一条的值
    form.setFieldsValue({
      name: row.name, enabled: row.enabled, boards: row.boards ?? [],
      row_filters_text: fmtKv(row.row_filters), hidden_text: (row.hidden_fields ?? []).join('\n'),
      note: row.note ?? '',
    });
    setOpen(true);
  };

  const submit = async () => {
    const v = await form.validateFields();
    const payload = {
      name: v.name,
      enabled: v.enabled,
      boards: v.boards ?? [],
      row_filters: parseKv(v.row_filters_text ?? ''),
      hidden_fields: (v.hidden_text ?? '').split('\n').map((s: string) => s.trim()).filter(Boolean),
      note: v.note ?? '',
    };
    try {
      if (editing) {
        await apiClient.put(`/bi/access/profiles/${editing.key}`, payload);
        message.success('已保存');
      } else {
        const { data } = await apiClient.post('/bi/access/profiles', payload);
        setTokenShown({ key: data?.key, token: data?.token });     // 一次性展示
      }
      setOpen(false);
      load();
    } catch (e: any) {
      message.error(e?.response?.data?.detail?.message ?? '保存失败');
    }
  };

  const rotate = async (row: Profile) => {
    const { data } = await apiClient.post(`/bi/access/profiles/${row.key}/rotate`);
    setTokenShown({ key: row.key, token: data?.token });
    load();
  };

  const remove = async (row: Profile) => {
    await apiClient.delete(`/bi/access/profiles/${row.key}`);
    message.success('已删除（令牌立即失效）');
    load();
  };

  const toggle = async (row: Profile, enabled: boolean) => {
    await apiClient.put(`/bi/access/profiles/${row.key}`, { enabled });
    load();
  };

  const screenLink = (row: Profile) => {
    const board = (row.boards ?? [])[0];
    const token = tokenShown?.key === row.key
      ? tokenShown.token
      : `${row.token_prefix}…（令牌仅在创建/轮换时可复制）`;
    const qs = new URLSearchParams();
    if (board) qs.set('board', board);
    qs.set('token', token);
    return `${window.location.origin}/bi/screen?${qs.toString()}`;
  };

  return (
    <div style={{ padding: 16 }}>
      <Space style={{ marginBottom: 12 }}>
        <Typography.Text strong>访问授权</Typography.Text>
        <Tooltip title="只读令牌：只能看授权范围内的大屏与数据，不能改任何配置">
          <Tag color="blue">只读</Tag>
        </Tooltip>
        <Button icon={<PlusOutlined />} type="primary" onClick={openCreate}>新建授权</Button>
        <Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>
        <Button onClick={() => navigate('/bi')}>返回看板</Button>
      </Space>

      <Alert
        type="info" showIcon style={{ marginBottom: 12 }}
        message="范围在服务端强制生效：看不到的看板/卡片根本不会返回；行过滤字段必须是卡片维度之一，否则该卡对这份授权不可见（宁可少给，不可多漏）。"
      />

      <Table<Profile>
        rowKey="key" size="small" loading={loading} dataSource={rows}
        pagination={false}
        columns={[
          { title: '名称', dataIndex: 'name',
            render: (v, r) => <Space>{v}{!r.enabled && <Tag>已停用</Tag>}</Space> },
          { title: '令牌', dataIndex: 'token_prefix',
            render: (v) => <Typography.Text code>{v}…</Typography.Text> },
          { title: '可见看板', dataIndex: 'boards',
            render: (v: string[]) => (v?.length
              ? v.map((b) => <Tag key={b} color="geekblue">{b}</Tag>)
              : <Tag color="orange">全部看板</Tag>) },
          { title: '行过滤', dataIndex: 'row_filters',
            render: (v: Record<string, string>) => (Object.keys(v ?? {}).length
              ? Object.entries(v).map(([k, val]) => <Tag key={k}>{k}={val}</Tag>)
              : <span style={{ color: '#9ca3af' }}>无（可见全部行）</span>) },
          { title: '隐藏列', dataIndex: 'hidden_fields',
            render: (v: string[]) => (v?.length
              ? v.map((f) => <Tag key={f}>{f}</Tag>)
              : <span style={{ color: '#9ca3af' }}>无</span>) },
          { title: '最近使用', dataIndex: 'last_used_at',
            render: (v) => (v ? v.replace('T', ' ').slice(0, 19) : '—') },
          { title: '操作', width: 260,
            render: (_, r) => (
              <Space size={4}>
                <Button size="small" type="link" onClick={() => openEdit(r)}>编辑</Button>
                <Popconfirm title="轮换后旧令牌立即失效，确认？" onConfirm={() => rotate(r)}>
                  <Button size="small" type="link" icon={<SyncOutlined />}>轮换</Button>
                </Popconfirm>
                <Button size="small" type="link"
                        onClick={async () => {
            // http 部署下 navigator.clipboard 不存在（可选链静默跳过=假成功）：
            // 失败回退 execCommand，再失败则弹窗展示让用户手动复制（审查 P3）
            const link = screenLink(r);
            try {
              if (navigator.clipboard?.writeText) {
                await navigator.clipboard.writeText(link);
              } else {
                const ta = document.createElement('textarea');
                ta.value = link;
                document.body.appendChild(ta);
                ta.select();
                const done = document.execCommand('copy');
                document.body.removeChild(ta);
                if (!done) throw new Error('copy failed');   // 回退失败不再假成功
              }
              message.success('已复制投放链接（令牌仅创建/轮换时可复制明文）');
            } catch {
              Modal.info({ title: '请手动复制投放链接', content: <Input.TextArea readOnly rows={2} value={link} /> });
            }
          }}>
                  <CopyOutlined />投放链接
                </Button>
                <Switch size="small" checked={r.enabled}
                        onChange={(v) => toggle(r, v)} />
                <Popconfirm title="删除后令牌立即失效，确认？" onConfirm={() => remove(r)}>
                  <Button size="small" type="link" danger icon={<DeleteOutlined />} />
                </Popconfirm>
              </Space>
            ) },
        ]}
      />

      <Modal open={open} title={editing ? '编辑授权' : '新建授权'} onCancel={() => setOpen(false)}
             onOk={submit} okText="保存" width={560}>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input placeholder="如：一车间大屏" />
          </Form.Item>
          <Form.Item name="boards" label="可见看板（不选=全部看板）">
            <Select mode="multiple" allowClear placeholder="全部看板"
                    options={boards.map((b) => ({ value: b.key, label: b.name }))} />
          </Form.Item>
          <Form.Item name="row_filters_text" label="行过滤（每行一个「字段:值」，多行 AND）"
                     extra="字段必须是卡片维度之一（如 line:A线）；否则该卡片对本授权不可见">
            <Input.TextArea rows={3} placeholder={'line:A线\nshift:早班'} />
          </Form.Item>
          <Form.Item name="hidden_text" label="隐藏列（每行一个字段；度量 key / 维度字段 / 明细列名）"
                     extra="被隐藏的字段在接口响应里不存在（服务端剥离）">
            <Input.TextArea rows={2} placeholder={'cost\nprofit'} />
          </Form.Item>
          <Form.Item name="note" label="备注">
            <Input />
          </Form.Item>
          <Form.Item name="enabled" label="启用" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>

      <Modal open={!!tokenShown} title="令牌（仅此一次展示，请立即保存）"
             onCancel={() => setTokenShown(null)} footer={null} width={560}>
        <Alert type="warning" showIcon style={{ marginBottom: 8 }}
               message="关闭本窗口后将无法再查看明文；忘记可用「轮换」重新生成。" />
        <Input.TextArea readOnly rows={2} value={tokenShown?.token ?? ''} />
      </Modal>

      <Card size="small" style={{ marginTop: 16 }}>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          投放链接示例：把「{'{服务端地址}'}/bi/screen?board={'{看板}'}&token={'{令牌}'}」发给对方浏览器打开即可；
          投放页会把令牌写入本地存储并自动从地址栏移除，之后刷新不再需要带参数。
        </Typography.Text>
      </Card>
    </div>
  );
};

export default BiAccessPage;
