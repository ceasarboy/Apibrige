export interface RefreshBatch {
  status: string;
  source_refreshable?: boolean;
}
export function refreshSourceDisabledReason(taskStatus: string | undefined, roundStatus: string | undefined, batch: RefreshBatch, busy: boolean): string | undefined {
  if (busy) return '正在处理，请稍候';
  if (taskStatus !== 'paused') return '请先暂停任务';
  if (!['blocked', 'retry_wait'].includes(roundStatus || '')) return '轮次仍在执行，请刷新进度';
  if (batch.source_refreshable === undefined) return '后端尚未返回刷新能力，请重启后端并刷新进度';
  if (!batch.source_refreshable) return '当前批次不可刷新，请刷新进度核对状态';
  return undefined;
}
