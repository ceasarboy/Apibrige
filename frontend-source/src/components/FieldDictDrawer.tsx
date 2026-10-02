import React, { useEffect, useRef, useState } from 'react';
import {
  Alert, Button, Checkbox, Drawer, Input, Select, Space, Table, Tag, message,
} from 'antd';
import { SaveOutlined } from '@ant-design/icons';
import apiClient from '../api/client';

export interface FieldDictConn {
  id: string;
  name: string;
  protocol: string;
  db_type?: string | null;
}

interface Props {
  /** null = 关闭抽屉 */
  conn: FieldDictConn | null;
  /** 打开即定位的表单：DB 连接当表名，HAP 连接当工作表 ID */
  formKey?: string;
  /** 抽屉标题里显示的表单名（缺省用 formKey） */
  formLabel?: string;
  onClose: () => void;
}

interface EditableField {
  path: string;
  field_id?: string;
  read_only?: boolean;
  name: string;
  type: string;
  nullable: boolean;
  primary_key: boolean;
  readable: boolean;
  writable: boolean;
}

/** 字段字典抽屉 — 查看/命名/读写勾选，按 连接+表单 存储
 *
 * 连接管理页（不带 formKey，自己选表或填工作表 ID）与表单注册表页
 * （带 formKey 直接定位）共用同一份字典：映射与探针读的也是它。
 */
const FieldDictDrawer: React.FC<Props> = ({ conn, formKey, formLabel, onClose }) => {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [data, setData] = useState<any>(null);
  const [fields, setFields] = useState<EditableField[]>([]);
  const [dbTables, setDbTables] = useState<string[]>([]);
  const [dbTable, setDbTable] = useState<string | undefined>(undefined);
  const [wsId, setWsId] = useState('');
  const [error, setError] = useState('');

  // 锁定模式：调用方（表单注册表）已给出具体表单，抽屉直接展示字段，无需再选表/填 ID
  const lockedKey = formKey?.trim() || '';
  const locked = !!lockedKey;

  const loadSeq = useRef(0);
  const load = async (c: FieldDictConn, params: { table?: string; worksheet_id?: string }) => {
    const seq = ++loadSeq.current;
    setLoading(true);
    setError('');
    try {
      const { data: res } = await apiClient.get(`/connections/${c.id}/fields`, { params });
      if (seq !== loadSeq.current) return;   // 晚到的旧表响应：丢弃，防止串表
      setData(res);
      if (res.kind === 'db' && Array.isArray(res.tables)) setDbTables(res.tables);
      setFields(Array.isArray(res.fields) ? res.fields.map((f: any) => ({
        path: f.path,
        field_id: f.field_id,
        read_only: !!f.read_only,
        name: f.name ?? '',
        type: f.display_type ? `${f.display_type} (${f.type})` : f.type,
        nullable: f.nullable,
        primary_key: f.primary_key,
        readable: f.readable !== false,
        writable: f.writable !== false,
      })) : []);
    } catch (err: any) {
      const msg = err?.response?.data?.detail || err?.message || '获取字段失败';
      setError(typeof msg === 'string' ? msg : JSON.stringify(msg));
      message.error(typeof msg === 'string' ? msg : msg?.message || '获取字段失败', 8);
      setData(null);
      setFields([]);
    } finally {
      setLoading(false);
    }
  };

  /** 按连接类型决定取字段的参数；类型未知（连接信息没加载到）返回 null */
  const paramsFor = (c: FieldDictConn, key: string)
    : { table?: string; worksheet_id?: string } | null => {
    if (c.db_type) return key ? { table: key } : {};
    if (c.protocol === 'hap') return key ? { worksheet_id: key } : null;
    return key ? null : {};
  };

  // 打开时定位：锁定模式直接拉该表单字段；否则 DB 先列表、HAP 等用户输入
  useEffect(() => {
    if (!conn) return;
    setData(null);
    setFields([]);
    setDbTables([]);
    setError('');
    setDbTable(conn.db_type ? lockedKey || undefined : undefined);
    setWsId(conn.protocol === 'hap' ? lockedKey : '');
    const params = paramsFor(conn, lockedKey);
    if (params === null) {
      if (lockedKey) setError('无法判断该表单的取字段方式（连接信息未就绪），请关闭后重试');
      return;
    }
    if (lockedKey || conn.db_type) load(conn, params);
  }, [conn?.id, formKey]);

  const [registering, setRegistering] = useState(false);
  const registerForm = async () => {
    const key = data?.table || data?.worksheet_id;
    if (!conn || !key) return;
    setRegistering(true);
    try {
      await apiClient.post('/forms', { connection_id: conn.id, form_key: key, display_name: formLabel || key });
      message.success('已增加到表单注册表');
    } catch { /* shared client reports conflict and errors */ }
    finally { setRegistering(false); }
  };

  const currentParams = (): { table?: string; worksheet_id?: string } | null => {
    if (!conn) return null;
    if (locked) {
      const p = paramsFor(conn, lockedKey);
      if (p === null) { message.warning('无法判断该表单的取字段方式'); return null; }
      return p;
    }
    if (conn.db_type) {
      if (!dbTable) { message.warning('请先选择数据表'); return null; }
      return { table: dbTable };
    }
    if (conn.protocol === 'hap') {
      if (!wsId.trim()) { message.warning('请输入工作表 ID'); return null; }
      return { worksheet_id: wsId.trim() };
    }
    return {};
  };

  const save = async () => {
    if (!conn) return;
    const params = currentParams();
    if (params === null) return;
    setSaving(true);
    try {
      const payload = { fields: fields.map((f) => ({
        path: f.path,
        field_id: f.field_id,
        read_only: !!f.read_only,
        name: f.name || null,
        readable: f.readable,
        writable: f.writable,
      })) };
      await apiClient.put(`/connections/${conn.id}/fields`, payload, { params });
      message.success(`已保存 ${payload.fields.length} 个字段定义`);
      await load(conn, params);
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      message.error(typeof detail === 'string' ? detail : detail?.message || '保存失败', 8);
    } finally {
      setSaving(false);
    }
  };

  const patch = (path: string, p: Partial<Pick<EditableField, 'name' | 'readable' | 'writable'>>) => {
    setFields((prev) => prev.map((f) => (f.path === path ? { ...f, ...p } : f)));
  };

  const columns = [
    {
      title: '字段名称', dataIndex: 'name', width: 200,
      render: (_: any, r: EditableField) => (
        <Input size="small" value={r.name ?? ''}
          onChange={(e) => patch(r.path, { name: e.target.value })} />
      ),
    },
    { title: '字段 ID / 列名', dataIndex: 'path',
      render: (v: string) => <code style={{ fontSize: 12 }}>{v}</code> },
    { title: '类型', dataIndex: 'type', width: 90,
      render: (v: string) => (v ? <Tag color="blue">{v}</Tag> : '-') },
    { title: '可读', dataIndex: 'readable', width: 70,
      render: (_: any, r: EditableField) => (
        <Checkbox checked={r.readable} onChange={(e) => patch(r.path, { readable: e.target.checked })} />
      ) },
    { title: '可写', dataIndex: 'writable', width: 70,
      render: (_: any, r: EditableField) => (
        <Checkbox disabled={r.read_only} checked={r.writable} onChange={(e) => patch(r.path, { writable: e.target.checked })} />
      ) },
    { title: '可空', dataIndex: 'nullable', width: 60,
      render: (v: boolean) => (v ? '是' : '否') },
    { title: '主键', dataIndex: 'primary_key', width: 60,
      render: (v: boolean) => (v ? '是' : '否') },
  ];

  const title = `字段设置 — ${conn?.name ?? ''}${formLabel || formKey ? ` / ${formLabel || formKey}` : ''}`;

  return (
    <Drawer title={title} open={!!conn} onClose={onClose} width={880}>
      {conn && (
        <>
          {locked ? (
            <div style={{ marginBottom: 16, fontSize: 12, color: 'var(--nb-text-muted)' }}>
              表单：<code>{lockedKey}</code>
              <Button size="small" type="link" loading={loading}
                onClick={() => load(conn, paramsFor(conn, lockedKey) || {})}>
                重新读取
              </Button>
            </div>
          ) : (
            <>
              {conn.protocol === 'hap' && (
                <Space style={{ marginBottom: 16 }}>
                  <Input
                    value={wsId}
                    onChange={(e) => setWsId(e.target.value)}
                    placeholder="请输入工作表 ID"
                    style={{ width: 320 }}
                    onPressEnter={() => load(conn, { worksheet_id: wsId.trim() })}
                  />
                  <Button type="primary" loading={loading}
                    onClick={() => load(conn, { worksheet_id: wsId.trim() })}>
                    获取字段
                  </Button>
                </Space>
              )}
              {!!conn.db_type && (
                <Space style={{ marginBottom: 16 }}>
                  <span>数据表：</span>
                  <Select
                    showSearch
                    value={dbTable}
                    placeholder="选择表后查看字段"
                    style={{ width: 320 }}
                    loading={loading && !dbTable}
                    options={dbTables.map((t) => ({ label: t, value: t }))}
                    onChange={(v) => { setDbTable(v); load(conn, { table: v }); }}
                  />
                </Space>
              )}
            </>
          )}

          {!locked && (data?.table || data?.worksheet_id) && (
            <Button style={{ marginBottom: 12 }} loading={registering} onClick={registerForm}>增加到表单注册表</Button>
          )}
          {error && (
            <Alert type="error" showIcon style={{ marginBottom: 12 }} message="读取字段失败"
              description={error} />
          )}
          {data?.kind === 'none' && <Alert type="info" showIcon message={data.hint} />}
          {data?.kind === 'hap' && data.hint && !data.fields && (
            <Alert type="info" showIcon message={data.hint} />
          )}

          {fields.length > 0 && (
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
              <span style={{ fontSize: 12, color: 'var(--nb-text-muted)' }}>
                共 {fields.length} 个字段。字典按「连接 + 表单」存储，名称用于展示；读写规则仅影响启用“字段字典过滤”的映射输出，不限制源系统读取。
              </span>
              <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={save}>
                保存字段定义
              </Button>
            </div>
          )}

          <Table
            rowKey="path"
            size="small"
            columns={columns}
            dataSource={fields}
            loading={loading}
            pagination={locked ? false : { pageSize: 20 }}
            scroll={locked ? { y: 520 } : undefined}
            locale={{
              emptyText: error ? ' '
                : locked ? '该表单未取到字段'
                  : conn.db_type ? '请先选择表'
                    : conn.protocol === 'hap' ? '输入工作表 ID 后获取'
                      : '该协议无自动字段定义',
            }}
          />
        </>
      )}
    </Drawer>
  );
};

export default FieldDictDrawer;
