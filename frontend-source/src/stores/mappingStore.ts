import { create } from 'zustand';
import apiClient from '../api/client';

export interface Mapping {
  id: string;
  name: string;
  source_connection_id: string;
  target_connection_id: string;
  source_endpoint: string | null;
  target_endpoint: string | null;
  source_table: string | null;
  target_table: string | null;
  mapping_config: Record<string, unknown>;
  transform_script: string | null;
  sync_mode: string;
  incremental_field: string | null;
  batch_size: number;
  status: string;
  created_at: string;
  updated_at: string;
}

interface MappingStore {
  mappings: Mapping[];
  loading: boolean;
  fetchMappings: () => Promise<void>;
  createMapping: (data: Record<string, unknown>) => Promise<Mapping>;
  updateMapping: (id: string, data: Record<string, unknown>) => Promise<Mapping>;
  deleteMapping: (id: string) => Promise<void>;
  previewMapping: (id: string, testData: Record<string, unknown>) => Promise<unknown>;
}

export const useMappingStore = create<MappingStore>((set) => ({
  mappings: [],
  loading: false,

  fetchMappings: async () => {
    set({ loading: true });
    try {
      const { data } = await apiClient.get('/mappings');
      set({ mappings: data });
    } finally {
      set({ loading: false });   // 失败也复位，避免永久转圈
    }
  },

  createMapping: async (data) => {
    const { data: mapping } = await apiClient.post('/mappings', data);
    set((s) => ({ mappings: [mapping, ...s.mappings] }));
    return mapping;
  },

  updateMapping: async (id, data) => {
    const { data: mapping } = await apiClient.put(`/mappings/${id}`, data);
    set((s) => ({ mappings: s.mappings.map((m) => (m.id === id ? mapping : m)) }));
    return mapping;
  },

  deleteMapping: async (id) => {
    await apiClient.delete(`/mappings/${id}`);
    set((s) => ({ mappings: s.mappings.filter((m) => m.id !== id) }));
  },

  previewMapping: async (id, testData) => {
    const { data } = await apiClient.post(`/mappings/${id}/preview`, { test_data: testData });
    return data;
  },
}));
