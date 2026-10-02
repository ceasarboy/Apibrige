import React, { useCallback, useEffect, useState } from 'react';
import {
  Card, Table, Button, Modal, Input, Descriptions, Tag, Space, message, Popconfirm,
} from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import apiClient from '../../api/client';

const levelColor: Record<string, string> = {
  L3: 'green',
  L2: 'blue',
  L3_partial: 'orange',
  L2_partial_no_contract: 'red',
  none: 'default',
};

/** 就地编辑映射（task 5.4）：以 "source -> target" 行文本编辑 */
function MappingEditor({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <Input.TextArea
      rows={6}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={'每行一条：源路径 -> 目标字段\n例如 user_name -> customer_name'}
    />
  );
}

const draftToText = (fields: Array<{ source: string; target: string }> | undefined): string =>
  (fields || []).map((f) => `${f.source} -> ${f.target}`).join('\n');

const textToDraft = (text: string) =>
  text
    .split('\n')
    .map((line) => line.split('->').map((s) => s.trim()))
    .filter((pair) => pair.length === 2 && pair[0] && pair[1])
    .map(([source, target]) => ({ source, target, transform: 'string' }));

const ProbesPage: React.FC = () => {
  const [rows, setRows] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [detail, setDetail] = useState<any | null>(null);
  const [draftText, setDraftText] = useState('');
  const [rejectOpen, setRejectOpen] = useState(false);
  const [rejectReason, setRejectReason] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await apiClient.get('/probes');
      setRows(data);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const openDetail = async (id: string) => {
    const { data } = await apiClient.get(`/probes/${id}`);
    setDetail(data);
    setDraftText(draftToText(data?.mapping_draft?.mapping_config?.fields));
  };

  const act = async (fn: () => Promise<any>, ok: string): Promise<boolean> => {
    try {
      await fn();
      message.success(ok);
      setDetail(null);
      load();
      return true;
    } catch {
      /* 拦截器已提示；返回 false 让调用方保持现场（如驳回弹窗不清空原因） */
      return false;
    }
  };

  const levelOk = (level: string) => level === 'L2' || level === 'L3';

  const approve = () =>
    act(
      () =>
        apiClient.post(`/probes/${detail.id}/approve`, {
          decided_by: 'admin',
          via: 'ui',
          mapping_config: {
            ...detail.mapping_draft?.mapping_config,
            fields: textToDraft(draftText),
          },
        }),
      '已批准并创建桥接',
    );

  return (
    <Card
      title="待确认清单"
      extra={
        <Button icon={<ReloadOutlined />} onClick={load}>
          刷新
        </Button>
      }
    >
      <Table
        rowKey="id"
        loading={loading}
        dataSource={rows}
        pagination={{ pageSize: 10 }}
        columns={[
          { title: '会话 ID', dataIndex: 'id', render: (v) => <code>{String(v).slice(0, 8)}</code> },
          {
            title: '源 → 目标',
            render: (_, r) =>
              `${r.source_snapshot?.base_url || '-'} → ${r.target_snapshot?.base_url || '-'}`,
          },
          {
            title: '协议',
            render: (_, r) => `${r.source_snapshot?.protocol || '-'} / ${r.target_snapshot?.protocol || '-'}`,
          },
          {
            title: '验证级别',
            dataIndex: 'level',
            render: (v: string) => <Tag color={levelColor[v] || 'default'}>{v}</Tag>,
          },
          { title: '创建时间', dataIndex: 'created_at' },
          {
            title: '操作',
            render: (_, r) => (
              <Button size="small" onClick={() => openDetail(r.id)}>
                查看对照
              </Button>
            ),
          },
        ]}
      />

      <Modal
        title={`会话对照 — ${detail ? String(detail.id).slice(0, 8) : ''}`}
        open={!!detail}
        onCancel={() => setDetail(null)}
        width={1080}
        footer={
          detail && (
            <Space>
              {/* task 5.4：级别 < L2 禁用批准并说明原因 */}
              {!levelOk(detail.level) && (
                <span style={{ color: '#faad14' }}>
                  验证级别 {detail.level || 'none'} 低于 L2，不允许批准
                </span>
              )}
              <Popconfirm
                title="批准并创建桥接？"
                description="将在单事务内创建连接、字段映射与同步任务"
                disabled={!levelOk(detail.level)}
                onConfirm={approve}
              >
                <Button type="primary" disabled={!levelOk(detail.level)}>
                  批准并建桥
                </Button>
              </Popconfirm>
              {/* task 5.5：驳回需填原因 */}
              <Button danger onClick={() => setRejectOpen(true)}>
                驳回
              </Button>
              {/* task 5.6：L3 授权二次确认，明示将写入真实数据 */}
              <Popconfirm
                title="授权端到端验证（L3）？"
                description="⚠️ 将向目标系统写入 1 条真实数据，确认继续？"
                okText="确认写入"
                okButtonProps={{ danger: true }}
                onConfirm={() =>
                  act(
                    () =>
                      apiClient.post(`/probes/${detail.id}/authorize-e2e`, {
                        authorized_by: 'admin',
                        via: 'ui',
                      }),
                    '已授权 L3',
                  )
                }
              >
                <Button danger>授权 L3 验证</Button>
              </Popconfirm>
            </Space>
          )
        }
      >
        {detail && (
          <>
            {/* task 5.3：三栏对照 */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12 }}>
              <Card size="small" title="源样本 / Schema">
                <pre style={{ maxHeight: 220, overflow: 'auto', fontSize: 12 }}>
                  {JSON.stringify(detail.source_sample, null, 1)}
                </pre>
                <pre style={{ maxHeight: 160, overflow: 'auto', fontSize: 12 }}>
                  {JSON.stringify(detail.source_schema?.fields, null, 1)}
                </pre>
              </Card>
              <Card size="small" title="映射草案（可就地编辑）">
                <MappingEditor value={draftText} onChange={setDraftText} />
              </Card>
              <Card size="small" title="目标 Schema 与差异">
                <pre style={{ maxHeight: 160, overflow: 'auto', fontSize: 12 }}>
                  {JSON.stringify(detail.target_schema?.fields, null, 1)}
                </pre>
                <pre style={{ maxHeight: 220, overflow: 'auto', fontSize: 12, color: '#cf1322' }}>
                  {JSON.stringify(detail.comparison, null, 1)}
                </pre>
              </Card>
            </div>
            <Descriptions size="small" column={3} style={{ marginTop: 12 }}>
              <Descriptions.Item label="状态">
                <Tag>{detail.status}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="级别">
                <Tag color={levelColor[detail.level] || 'default'}>{detail.level}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="L3 授权">
                {detail.e2e_authorized ? '已授权' : '未授权'}
              </Descriptions.Item>
            </Descriptions>
          </>
        )}
      </Modal>

      <Modal
        title="驳回（必填原因）"
        open={rejectOpen}
        onCancel={() => setRejectOpen(false)}
        onOk={async () => {
          // 成功才关闭并清空原因；失败保持弹窗与已输入原因（拦截器已提示）
          const okResult = await act(
            () =>
              apiClient.post(`/probes/${detail?.id}/reject`, {
                reason: rejectReason,
                decided_by: 'admin',
                via: 'ui',
              }),
            '已驳回',
          );
          if (okResult) {
            setRejectOpen(false);
            setRejectReason('');
          }
        }}
        okButtonProps={{ disabled: !rejectReason.trim() }}
      >
        <Input.TextArea
          rows={3}
          value={rejectReason}
          onChange={(e) => setRejectReason(e.target.value)}
          placeholder="请填写驳回原因（必填）"
        />
      </Modal>
    </Card>
  );
};

export default ProbesPage;
