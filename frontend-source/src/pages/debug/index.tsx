import React, { useState } from 'react';
import {
  Card, Input, Button, Steps, Descriptions, Tag, Space, message, Popconfirm,
} from 'antd';
import { RadarChartOutlined } from '@ant-design/icons';
import apiClient from '../../api/client';

/**
 * 联调探针控制台（task 5.7）
 * 替代原「API 调试」裸代理：全部调用走受控探针接口
 * （目标白名单 + 鉴权 + 预算 + 三级验证），产物进入待确认清单。
 */

interface ProbeState {
  id: string;
  status: string;
  level?: string;
  l1_result?: any;
  source_schema?: any;
  target_schema?: any;
  l2_result?: any;
}

const STEP_TITLES = ['建会话', 'L1 探测', 'Schema 推断', '映射草案', 'L2 验证', '提交清单'];

const ProbePage: React.FC = () => {
  const [sourceUrl, setSourceUrl] = useState('https://');
  const [sourceEndpoint, setSourceEndpoint] = useState('/users');
  const [sourceToken, setSourceToken] = useState('');
  const [targetUrl, setTargetUrl] = useState('https://');
  const [targetEndpoint, setTargetEndpoint] = useState('/customers');
  const [contractText, setContractText] = useState(
    '[{"name": "user_name", "type": "string"}, {"name": "age", "type": "integer"}]');
  const [session, setSession] = useState<ProbeState | null>(null);
  const [busy, setBusy] = useState(false);

  const stepIndex = (() => {
    if (!session) return 0;
    const s = session.status;
    if (s === 'draft') return 0;
    if (s === 'probing') return 1;
    if (s === 'L1_reachable') return 2;
    if (s === 'transform_verified') return 5;
    return 1;
  })();

  const run = async (fn: () => Promise<any>, okMsg: string) => {
    setBusy(true);
    try {
      const { data } = await fn();
      setSession(data);
      message.success(okMsg);
      return data;
    } catch (e: any) {
      const detail = e.response?.data?.detail;
      const code = typeof detail === 'object' ? detail?.code : undefined;
      message.error(code ? `${code}: ${detail?.message || ''}` : '调用失败');
      return null;
    } finally {
      setBusy(false);
    }
  };

  const createAndProbe = async () => {
    const created = await run(
      () =>
        apiClient.post('/probes', {
          source: {
            protocol: 'rest',
            base_url: sourceUrl,
            auth_type: sourceToken ? 'bearer' : undefined,
            auth_config: sourceToken ? { token: sourceToken } : undefined,
            endpoint: sourceEndpoint,
          },
          target: { protocol: 'rest', base_url: targetUrl, endpoint: targetEndpoint },
          via: 'ui',
        }),
      '会话已创建（draft）',
    );
    if (!created) return;
    await run(() => apiClient.post(`/probes/${created.id}/probe`, {}, { params: { via: 'ui' } }),
      'L1 完成');
  };

  const infer = () =>
    run(() => apiClient.post(`/probes/${session!.id}/infer`, {}, { params: { via: 'ui' } }),
      'Schema 已推断');

  const draft = () =>
    run(() => {
      let target_contract;
      try {
        target_contract = contractText.trim() ? JSON.parse(contractText) : undefined;
      } catch {
        message.error('目标契约不是合法 JSON');
        return Promise.reject(new Error('bad contract'));
      }
      return apiClient.post(`/probes/${session!.id}/draft`,
        { via: 'ui', target_contract });
    }, '映射草案已生成');

  const verifyL2 = () =>
    run(() => apiClient.post(`/probes/${session!.id}/verify`,
      { level: 'L2', via: 'ui' }), 'L2 验证完成');

  const submit = () =>
    run(() => apiClient.post(`/probes/${session!.id}/submit`,
      { via: 'ui', decided_by: 'ui-probe' }), '已提交待确认清单');

  return (
    <Card title="联调探针">
      <Card type="inner" title="1. 目标配置" style={{ marginBottom: 16 }}>
        <Space direction="vertical" style={{ width: '100%' }} size={8}>
          <Space.Compact style={{ width: '100%' }}>
            <Input style={{ width: '40%' }} value={sourceUrl}
              onChange={(e) => setSourceUrl(e.target.value)} placeholder="源系统 Base URL" />
            <Input style={{ width: '25%' }} value={sourceEndpoint}
              onChange={(e) => setSourceEndpoint(e.target.value)} placeholder="源端点" />
            <Input.Password style={{ width: '35%' }} value={sourceToken}
              onChange={(e) => setSourceToken(e.target.value)} placeholder="源认证 Token（可选）" />
          </Space.Compact>
          <Space.Compact style={{ width: '100%' }}>
            <Input style={{ width: '40%' }} value={targetUrl}
              onChange={(e) => setTargetUrl(e.target.value)} placeholder="目标系统 Base URL" />
            <Input style={{ width: '60%' }} value={targetEndpoint}
              onChange={(e) => setTargetEndpoint(e.target.value)} placeholder="目标端点" />
          </Space.Compact>
          <Input.TextArea rows={2} value={contractText}
            onChange={(e) => setContractText(e.target.value)}
            placeholder='目标契约（JSON）：[{"name":"字段","type":"string"}]' />
          <Popconfirm
            title="开始联调？"
            description="将按目标白名单与调用预算对源/目标执行探测"
            onConfirm={createAndProbe}
          >
            <Button type="primary" loading={busy} icon={<RadarChartOutlined />}>
              建会话并执行 L1
            </Button>
          </Popconfirm>
        </Space>
      </Card>

      <Card type="inner" title="2. 联调流程">
        <Steps
          size="small"
          current={stepIndex}
          items={STEP_TITLES.map((t) => ({ title: t }))}
        />
        {session && (
          <>
            <Descriptions size="small" column={3} style={{ marginTop: 16 }}>
              <Descriptions.Item label="会话">
                <code>{String(session.id).slice(0, 8)}</code>
              </Descriptions.Item>
              <Descriptions.Item label="状态">
                <Tag>{session.status}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="L1">
                {session.l1_result
                  ? `源 ${session.l1_result.source?.status} / 目标 ${session.l1_result.target?.status}`
                  : '-'}
              </Descriptions.Item>
            </Descriptions>
            <Space style={{ marginTop: 12 }} wrap>
              <Button onClick={infer} disabled={session.status !== 'L1_reachable'} loading={busy}>
                推断 Schema
              </Button>
              <Button onClick={draft} disabled={session.status !== 'L1_reachable'} loading={busy}>
                生成映射草案
              </Button>
              <Button type="primary" onClick={verifyL2}
                disabled={session.status !== 'L1_reachable'} loading={busy}>
                执行 L2 验证（不写目标）
              </Button>
              <Button onClick={submit}
                disabled={session.status !== 'transform_verified'} loading={busy}>
                提交待确认清单
              </Button>
            </Space>
            {session.l2_result && (
              <pre style={{ marginTop: 12, maxHeight: 200, overflow: 'auto', fontSize: 12 }}>
                {JSON.stringify(session.l2_result, null, 1)}
              </pre>
            )}
          </>
        )}
      </Card>
    </Card>
  );
};

export default ProbePage;
