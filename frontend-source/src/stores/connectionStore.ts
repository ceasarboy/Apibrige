import { create } from 'zustand';
import apiClient from '../api/client';

export interface Connection {
  id: string;
  name: string;
  connection_type: string;
  protocol: string;
  base_url: string | null;
  auth_type: string | null;
  auth_config: Record<string, string> | null;
  headers: Record<string, string> | null;
  timeout_ms: number;
  retry_count: number;
  db_type: string | null;
  db_config: Record<string, unknown> | null;
  status: string;
  created_at: string;
  updated_at: string;
}

interface ConnectionStore {
  connections: Connection[];
  loading: boolean;
  fetchConnections: (type?: string) => Promise<void>;
  createConnection: (data: Record<string, unknown>) => Promise<Connection>;
  updateConnection: (id: string, data: Record<string, unknown>) => Promise<Connection>;
  deleteConnection: (id: string) => Promise<void>;
  testConnection: (id: string) => Promise<{ status: string; latency_ms: number; error?: string }>;
}

export const useConnectionStore = create<ConnectionStore>((set) => ({
  connections: [],
  loading: false,

  fetchConnections: async (type) => {
    set({ loading: true });
    try {
      const params = type ? { connection_type: type } : {};
      const { data } = await apiClient.get('/connections', { params });
      set({ connections: data });
    } finally {
      // 失败也复位：不复位会让列表永久转圈（报告/抽检都进不来）
      set({ loading: false });
    }
  },

  createConnection: async (data) => {
    const { data: conn } = await apiClient.post('/connections', data);
    set((s) => ({ connections: [conn, ...s.connections] }));
    return conn;
  },

  updateConnection: async (id, data) => {
    const { data: conn } = await apiClient.put(`/connections/${id}`, data);
    set((s) => ({
      connections: s.connections.map((c) => (c.id === id ? conn : c)),
    }));
    return conn;
  },

  deleteConnection: async (id) => {
    await apiClient.delete(`/connections/${id}`);
    set((s) => ({ connections: s.connections.filter((c) => c.id !== id) }));
  },

  testConnection: async (id) => {
    const { data } = await apiClient.post(`/connections/${id}/test`);
    return data;
  },
}));
