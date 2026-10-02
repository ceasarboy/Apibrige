# add-bi-hap-incremental 任务清单

## 1. 适配器（app/adapters/hap.py）

- [ ] `get_worksheet_rows` 增可选 `filters` / `sort` / `return_total`（复用同一翻页循环，
      不新开请求路径）——验证：单测（假 httpx 响应）+ 真实端点探针
- [ ] `get_rows_changed_since(worksheet_id, since, *, overlap_seconds)`：`utime gt (since-overlap)`
      + `utime desc`，返回 `(rows, total)`——验证：真实端点（新增/改行后只回传变化行）
- [ ] 参数名陷阱固化：`filters` 有效、`filterControls` 被静默忽略（注释 + 探针脚本留档）

## 2. 快照与策略（app/services/bi_sources.py）

- [ ] `_hap_snapshots` 快照存储（rowid 索引 / watermark / total / full_at / 计数）
      ——验证：单测
- [ ] HAP 分支策略：全量条件（首轮/对账超时/无水位线/缺字段/超阈值）与增量合并
      （2s 重叠 + 幂等 upsert）——验证：单测矩阵 10 例
- [ ] 配置常量 + 环境变量覆写（MIN_ROWS / RECONCILE / OVERLAP / DELTA_CAP / 总开关）
      ——验证：单测（开关关闭 → 全量）
- [ ] 上限语义不变：合并后按 MAX_FORM_ROWS 截断 + 告警——验证：单测

## 3. 可观测性（app/api/bi.py）

- [ ] `GET /bi/form-rows` 响应附 `snapshot: {mode, delta_rows, total_rows, watermark, full_at_ago_s}`
      ——验证：API 测试 + 真实端点验证脚本
- [ ] `_scan_form` 增量日志一行——验证：日志断言（caplog）

## 4. 验收

- [ ] 真实端点验证脚本 `scripts/hap_incremental_verify.py`：阈值调 0 → 首轮全量 →
      新增 2 行/改 1 行 → 增量轮次只回传 3 行（<5%）→ 合并快照 == 全量拉取逐行一致 →
      删除 1 行 → 对账轮后消失 → 自清理——验证：脚本输出
- [ ] 全量 pytest 零回归 + 5 个 UI 自审脚本零回归——验证：命令输出
- [ ] 文档（project-status / bi-evolution-plan / skill 大表能力节）+ OpenSpec 归档
      ——验证：文档 diff
