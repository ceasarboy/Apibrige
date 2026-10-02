import { create } from 'zustand';
import apiClient from '../api/client';

export interface Log {
  id: string;
  task_id: string;
  buffer_id: string | null;
  action: string;
  status: string;
  request_data: Record<string, unknown> | null;
  response_data: Record<string, unknown> | null;
  error_message: string | null;
  duration_ms: number | null;
  created_at: string;
}

interface LogStore {
  logs: Log[];
  total: number;
  loading: boolean;
  _fetchSeq: number;
  fetchLogs: (params?: Record<string, unknown>) => Promise<void>;
}

export const useLogStore = create<LogStore>((set, get) => ({
  logs: [],
  total: 0,
  loading: false,

  // 请求序号（审查 P2 #32）：快速切换筛选/翻页时，晚到的旧响应不得覆盖新状态
  _fetchSeq: 0,
  fetchLogs: async (params = {}) => {
    const seq = ++get()._fetchSeq;
    set({ loading: true });
    try {
      const { data } = await apiClient.get('/logs', { params });
      if (seq === get()._fetchSeq) set({ logs: data.items, total: data.total });
    } finally {
      if (seq === get()._fetchSeq) set({ loading: false });   // 失败也复位，避免永久转圈
    }
  },
}));
