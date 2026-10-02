import React, { useEffect, useState } from 'react';
import { Outlet, useNavigate, useLocation } from 'react-router-dom';
import { Layout, Menu, Popover, Input, Button, message } from 'antd';
import type { MenuProps } from 'antd';
import {
  ApiOutlined,
  NodeIndexOutlined,
  ScheduleOutlined,
  FileTextOutlined,
  DashboardOutlined,
  ExperimentOutlined,
  SendOutlined,
  KeyOutlined,
  FileDoneOutlined,
} from '@ant-design/icons';
import apiClient from '../api/client';
import { getStoredToken, setStoredToken } from '../api/client';

const { Sider, Content } = Layout;

const menuItems: MenuProps['items'] = [
  { key: '/dashboard', icon: <DashboardOutlined />, label: '监控看板' },
  { key: '/connections', icon: <ApiOutlined />, label: '连接管理' },
  { key: '/forms', icon: <FileTextOutlined />, label: '表单注册表' },
  { key: '/mappings', icon: <NodeIndexOutlined />, label: '字段映射' },
  { key: '/tasks', icon: <ScheduleOutlined />, label: '同步任务' },
  { key: '/logs', icon: <FileTextOutlined />, label: '执行日志' },
  { key: '/testing', icon: <ExperimentOutlined />, label: '收发比对' },
  { key: '/debug', icon: <SendOutlined />, label: '联调探针' },
  { key: '/probes', icon: <FileDoneOutlined />, label: '待确认清单' },
];

/** 管理 Token 输入面板（task 2.4） */
const TokenPanel: React.FC<{ onSaved: () => void }> = ({ onSaved }) => {
  const [token, setToken] = useState(getStoredToken());

  const save = () => {
    setStoredToken(token);
    message.success(token ? 'Token 已保存' : 'Token 已清空');
    onSaved();
  };

  return (
    <div style={{ width: 280 }}>
      <div style={{ fontSize: 12, color: '#666', marginBottom: 8 }}>
        管理接口需要携带 APIBRIDGE_ADMIN_TOKEN 配置的共享 Token。
      </div>
      <Input.Password
        value={token}
        placeholder="粘贴管理 Token"
        onChange={(e) => setToken(e.target.value)}
        onPressEnter={save}
      />
      <Button type="primary" size="small" block style={{ marginTop: 8 }} onClick={save}>
        保存
      </Button>
    </div>
  );
};

const MainLayout: React.FC = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const [tokenVersion, setTokenVersion] = useState(0);
  const [biEnabled, setBiEnabled] = useState(false);
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem('nb_sider_collapsed') === '1');
  useEffect(() => {
    if (!getStoredToken()) return;
    let active = true;
    apiClient.get('/bi/status').then(({ data }) => { if (active) setBiEnabled(!!data.enabled); }).catch(() => {});
    return () => { active = false; };
  }, [tokenVersion]);
  const selectedKey = '/' + location.pathname.split('/')[1];

  return (
    <Layout style={{ minHeight: '100vh', background: '#FFFFFF' }}>
      <Sider
        className="nb-sider"
        collapsible
        collapsed={collapsed}
        collapsedWidth={64}
        onCollapse={(value) => { setCollapsed(value); localStorage.setItem('nb_sider_collapsed', value ? '1' : '0'); }}
        width={220}
        style={{
          background: 'var(--nb-sidebar)',
          borderRight: '1px solid var(--nb-border)',
        }}
      >
        {/* Logo */}
        <div style={{
          padding: '20px 16px 16px',
          display: 'flex',
          alignItems: 'center',
          gap: 10,
        }}>
          <div style={{
            width: 28,
            height: 28,
            borderRadius: 6,
            background: 'linear-gradient(135deg, var(--nb-primary), #5BA0F2)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: '#fff',
            fontSize: 14,
            fontWeight: 700,
          }}>
            A
          </div>
          <span style={{
            fontSize: 15,
            fontWeight: 600,
            color: 'var(--nb-text)',
            letterSpacing: '-0.3px',
          }}>
            APIBridge
          </span>
        </div>

        {/* Navigation */}
        <div style={{ padding: '0 4px' }}>
          <div style={{
            padding: '4px 12px 6px',
            fontSize: 11,
            fontWeight: 600,
            color: 'var(--nb-text-muted)',
            textTransform: 'uppercase',
            letterSpacing: '0.8px',
          }}>
            导航
          </div>
          <Menu
            style={{ background: 'transparent', border: 'none' }}
            theme="dark"
            mode="inline"
            selectedKeys={[selectedKey]}
            items={biEnabled ? [menuItems![0], { key: '/bi', icon: <DashboardOutlined />, label: 'BI 大屏' }, ...menuItems!.slice(1)] : menuItems}
            onClick={({ key }) => navigate(key)}
          />
        </div>
      </Sider>

      <Layout style={{ background: 'var(--nb-bg)' }}>
        {/* 顶栏：管理 Token 入口（task 2.4） */}
        <div style={{
          height: 44,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'flex-end',
          padding: '0 24px',
          borderBottom: '1px solid var(--nb-border)',
          background: '#fff',
        }}>
          <Popover
            content={<TokenPanel onSaved={() => setTokenVersion((v) => v + 1)} />}
            trigger="click"
            placement="bottomRight"
          >
            <Button size="small" icon={<KeyOutlined />}>
              Token{getStoredToken() ? '' : '（未配置）'}
              <span data-token-version={tokenVersion} />
            </Button>
          </Popover>
        </div>
        <Content style={{
          padding: '28px 32px',
          minHeight: 'calc(100vh - 44px)',
          overflow: 'auto',
        }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
};

export default MainLayout;
