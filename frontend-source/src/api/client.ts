import axios from 'axios';
import { message } from 'antd';

const TOKEN_STORAGE_KEY = 'apibridge_admin_token';
// BI 只读访问令牌（add-bi-access-scope）：投放页专用；管理页仍用管理令牌
const BI_TOKEN_STORAGE_KEY = 'apibridge_bi_token';

export function getStoredToken(): string {
  return localStorage.getItem(TOKEN_STORAGE_KEY) || '';
}

export function setStoredToken(token: string): void {
  localStorage.setItem(TOKEN_STORAGE_KEY, token.trim());
}

export function getStoredBiToken(): string {
  return localStorage.getItem(BI_TOKEN_STORAGE_KEY) || '';
}

export function setStoredBiToken(token: string): void {
  if (token.trim()) localStorage.setItem(BI_TOKEN_STORAGE_KEY, token.trim());
  else localStorage.removeItem(BI_TOKEN_STORAGE_KEY);
}

/** 启动时导入地址栏里的只读令牌（投放链接 `?token=bit_…`）
 *
 * 必须在**首个 API 请求之前**执行：投放页被 BiGate 挡住时会先请求 /bi/status，
 * 若此时令牌还没入 localStorage，该请求就是未鉴权 → 门禁失败 → 投放页永远不挂载，
 * 页面 effect 里的令牌注入永不执行（实测死锁）。故上移到应用启动阶段。
 * 导入后抹掉地址栏参数（避免历史/截图/转发带出令牌），之后走 localStorage。
 */
export function bootstrapBiTokenFromUrl(): void {
  try {
    const url = new URL(window.location.href);
    const token = url.searchParams.get('token');
    if (!token) return;
    setStoredBiToken(token);
    url.searchParams.delete('token');
    window.history.replaceState({}, '', url.toString());
  } catch {
    /* 非法 URL 等场景忽略：不影响管理页正常使用 */
  }
}

// 自定义请求标记：拦截器据此跳过 message.error（候选值缺失等静默请求）
declare module 'axios' {
  export interface AxiosRequestConfig {
    skipErrorMessage?: boolean;
  }
}

const apiClient = axios.create({
  baseURL: '/api/v1',
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
});

// 请求拦截器：注入管理 token（task 2.4）
apiClient.interceptors.request.use((config) => {
  // 同步运行/恢复会等待整轮执行；由服务端控制单次外部请求超时。
  // 页面不能在 30 秒后放弃等待并把仍在执行的任务误报为失败。
  if (config.method === 'post' && /^(\/tasks\/[^/]+\/(run|hap-retry|round\/retry)|\/testing\/(stress|send))$/.test(config.url || '')) {
    // 后端同步阻塞到整段时长结束（压测 UI 允许 60s > axios 30s），不能被客户端超时截断
    config.timeout = 0;
  }
  const token = getStoredToken();
  if (token) {
    config.headers['X-Admin-Token'] = token;
  } else {
    // 未配置管理令牌时用 BI 只读令牌（投放页场景）
    const biToken = getStoredBiToken();
    if (biToken) config.headers['X-BI-Token'] = biToken;
  }
  return config;
});

// 响应拦截器：统一错误处理；401 时给出可操作的提示而非静默失败（task 2.4）
apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error.response?.status;
    const detail = error.response?.data?.detail;
    // 后台静默请求（如筛选栏候选值）：失败不弹提示，由调用方降级处理
    if ((error.config as any)?.skipErrorMessage) {
      return Promise.reject(error);
    }
    if (status === 401) {
      const code = typeof detail === 'object' ? detail?.code : undefined;
      if (code === 'admin_token_missing') {
        message.error('服务端未配置 APIBRIDGE_ADMIN_TOKEN，生产环境拒绝访问');
      } else if (!getStoredToken()) {
        message.error('未配置管理 Token：请在页面右上角「Token」中填写后重试');
      } else {
        message.error('管理 Token 无效：请在页面右上角「Token」中更新后重试');
      }
    } else {
      const msg =
        (typeof detail === 'object' ? detail?.message : detail) ||
        error.message ||
        '请求失败';
      message.error(msg);
    }
    return Promise.reject(error);
  },
);

export default apiClient;
