import { create } from 'zustand';
import apiClient from '../api/client';

export interface Task {
  id: string;
  mapping_id: string;
  task_type: string;
  schedule_cron: string | null;
  webhook_config: Record<string, unknown> | null;
  status: string;
  sync_mode: string;
  fallback_to_async: boolean;
  auto_threshold: number;
  last_run_at: string | null;
  next_run_at: string | null;
  last_sync_value: string | null;
  active_round_id?: string | null;
  checkpoint_version?: number;
  created_at: string;
  updated_at: string;
}

interface TaskStore {
  tasks: Task[];
  loading: boolean;
  fetchTasks: (status?: string) => Promise<void>;
  createTask: (data: Record<string, unknown>) => Promise<Task>;
  updateTask: (id: string, data: Record<string, unknown>) => Promise<Task>;
  deleteTask: (id: string) => Promise<void>;
  runTask: (id: string) => Promise<unknown>;
  pauseTask: (id: string) => Promise<void>;
  resumeTask: (id: string) => Promise<void>;
}

export const useTaskStore = create<TaskStore>((set) => ({
  tasks: [],
  loading: false,

  fetchTasks: async (status) => {
    set({ loading: true });
    try {
      const params = status ? { status } : {};
      const { data } = await apiClient.get('/tasks', { params });
      set({ tasks: data });
    } finally {
      set({ loading: false });   // 失败也复位，避免永久转圈
    }
  },

  createTask: async (data) => {
    const { data: task } = await apiClient.post('/tasks', data);
    set((s) => ({ tasks: [task, ...s.tasks] }));
    return task;
  },

  updateTask: async (id, data) => {
    const { data: task } = await apiClient.put(`/tasks/${id}`, data);
    set((s) => ({ tasks: s.tasks.map((t) => (t.id === id ? task : t)) }));
    return task;
  },

  deleteTask: async (id) => {
    await apiClient.delete(`/tasks/${id}`);
    set((s) => ({ tasks: s.tasks.filter((t) => t.id !== id) }));
  },

  runTask: async (id) => {
    const { data } = await apiClient.post(`/tasks/${id}/run`);
    return data;
  },

  pauseTask: async (id) => {
    await apiClient.post(`/tasks/${id}/pause`);
    set((s) => ({
      tasks: s.tasks.map((t) => (t.id === id ? { ...t, status: 'paused' } : t)),
    }));
  },

  resumeTask: async (id) => {
    await apiClient.post(`/tasks/${id}/resume`);
    set((s) => ({
      tasks: s.tasks.map((t) => (t.id === id ? { ...t, status: 'active' } : t)),
    }));
  },
}));
