# Field Dictionary 变更

## Delta Specifications

- [x] [connection-api](specs/connection-api/spec.md)
- [x] [data-pipeline](specs/data-pipeline/spec.md)
- [x] [management-ui](specs/management-ui/spec.md)

## Tasks

## 1. 字段字典模型与工具

- [x] 1.1 新增 `ConnectionField` 模型（connection_id + scope + path + name + readable + writable，三元唯一，默认双可读写）→ 验证: tests/test_field_dictionary.py TestLoadFieldDefs 落库读写/scope 隔离断言过
- [x] 1.2 创建 `app/services/field_dict.py`：`load_field_defs(conn_id, scope)` 读取字典、`apply_field_privacy(records, defs, mode)` 按 mode=source/target 过滤不可读/不可写字段 → 验证: compute_drop_keys/filter_records 纯函数用例（TestComputeDropKeys 8 例、TestFilterRecords 5 例）全过，含大小写互认与容器形状保持

## 2. 字段字典 API

- [x] 2.1 `PUT /connections/{id}/fields`（DB 按 table、HAP 按 worksheet_id 定 scope）批量 upsert 字段字典 → 验证: test_put_creates_default_readable_writable（新增默认双可读写 + 回读 saved）与 test_put_updates_only_passed_attrs（只更新传入属性）过
- [x] 2.2 增强 `GET /connections/{id}/fields` 合并字典：name/readable/writable 用字典值覆盖，未保存字段默认双勾选，响应带 `saved` 标记 → 验证: test_get_merges_saved_dict / test_get_case_insensitive_merge 过

## 3. 同步读写过滤

- [x] 3.1 `sync_handler` 源侧过滤：转换后按源连接字段字典剔除 readable=false 字段再算 records_full/键集 → 验证: test_source_unreadable_field_dropped（推送载荷无 ctrl_key）+ test_delete_sync_skipped_when_key_unreadable（key 被禁读 → 删除同步安全跳过）过
- [x] 3.2 `sync_handler` 目标侧过滤：推送前剔除 writable=false 字段；无字典行为不变 → 验证: test_target_unwritable_field_dropped（载荷无 email）+ test_no_dict_no_regression（全字段推送）过

## 4. 前端字段抽屉

- [x] 4.1 字段抽屉表格行内编辑：name Input + readable/writable Checkbox（默认勾选）→ 验证: connections/index.tsx 行内 Input/Checkbox 渲染 + 副本回显；npx tsc --noEmit 零错误
- [x] 4.2 「保存字段定义」按钮：收集当前表字段 → PUT 保存 → 成功提示 → 刷新读取状态 → 验证: saveFields 走 PUT 并回读 loadFields；API 契约由 TestClient 用例覆盖（浏览器走查待办）
- [x] 4.3 `npx tsc --noEmit` 类型检查通过 → 验证: 无类型错误（已清 tsconfig.tsbuildinfo 再跑）

## 5. 测试与回归

- [x] 5.1 新增 `tests/test_field_dictionary.py` 覆盖 API 保存/合并 + 同步读写过滤；全量 `pytest tests/ -q` 通过 → 验证: 全量 532 passed / 11 skipped（基线 465 + 本轮 67 新增）