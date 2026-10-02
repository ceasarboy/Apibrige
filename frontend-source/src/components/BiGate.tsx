import React, { useEffect, useRef, useState } from 'react';
import { Alert, Spin } from 'antd';
import apiClient from '../api/client';

const RETRY_DELAY_MS = 5000;
const MAX_RETRIES = 6;

export default function BiGate({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<'loading' | 'on' | 'off' | 'error'>('loading');
  // 错误态有限重试（审查 P3）：大屏开机瞬间后端未起/网络抖动时自动恢复，
  // 不再需要人工刷页；重试耗尽才停在提示
  const retries = useRef(0);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const check = () => {
      apiClient.get('/bi/status').then(({ data }) => {
        if (active) setState(data.enabled ? 'on' : 'off');
      }).catch(() => {
        if (!active) return;
        if (retries.current < MAX_RETRIES) {
          retries.current += 1;
          timer = setTimeout(check, RETRY_DELAY_MS);
        } else {
          setState('error');
        }
      });
    };
    check();
    return () => { active = false; if (timer) clearTimeout(timer); };
  }, []);
  if (state === 'loading') return <Spin />;
  if (state !== 'on') {
    return (
      <Alert type="info" showIcon
        message={state === 'off'
          ? 'BI 尚未启用，请联系管理员'
          : '无法读取 BI 状态（已自动重试），请检查登录与连接后刷新'} />
    );
  }
  return <>{children}</>;
}
