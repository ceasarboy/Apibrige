# 监控看板增强 变更

## Delta Specifications

- [x] [monitoring-dashboard](specs/monitoring-dashboard/spec.md)
- [x] [data-pipeline](specs/data-pipeline/spec.md)

## Tasks

## 1. 先建守卫测试（让缺口显性化）

- [x] 1.1 新增 `tests/test_metrics_export.py`：断言 `/metrics` 输出包含关键指标
  （`apibridge_sync_total`、`apibridge_sync_duration_seconds_bucket`、`apibridge_buffer_pending`、
  `apibridge_active_tasks`、`apibridge_active_connections`、`apibridge_db_pool_connections`、
  `process_resident_memory_bytes`、`process_cpu_seconds_total`）
  → 验证: 测试在改动前**先失败**（红），失败清单即"仅声明未赋值"的完整缺口表
- [x] 1.2 新增守卫：解析 `configs/grafana-dashboard.json` 与 `configs/alerts.yml`，提取其中引用的
  `apibridge_*` / `process_*` 指标名，断言每一个都能在 `/metrics` 输出中找到
  → 验证: 测试输出逐条缺口（面板/规则 → 指标名 → 是否存在）

## 2. 指标链路修复（逐项消掉 1.x 暴露的缺口）

- [x] 2.1 `app/metrics_collector.py` 显式注册 `ProcessCollector(registry=registry)` 与
  `PlatformCollector(registry=registry)`（D3：保留自定义 registry 隔离）
  → 验证: `/metrics` 输出含 `process_resident_memory_bytes`、`process_cpu_seconds_total`
- [x] 2.2 新增 `apibridge_buffer_pending` Gauge，并在 `/metrics` 端点内 pull 时刷新（D1）
  → 验证: 单测断言其值等于 `data_buffer` 中 `pending`+`processing` 行数（构造固定数据）
- [x] 2.3 实现 `apibridge_queue_depth` 真实读取：aio_pika passive declare 取就绪消息数（不消费），
  MQ 不可达/队列不存在时**不导出**该序列（D2）
  → 验证: 单测覆盖 MQ 可达 / 不可达两分支；无 MQ 环境下断言序列缺失且 `/metrics` 仍返回 200
- [x] 2.4 赋值 `apibridge_active_tasks`、`apibridge_active_connections`、
  `apibridge_db_pool_connections`、`apibridge_db_pool_size`（同一刷新路径）
  → 验证: 单测构造活跃任务/连接/连接池后断言 Gauge 反映真实数量（非 0、非缺失）
- [x] 2.5 刷新失败降级：DB 或 MQ 异常时仅记日志、不更新对应 Gauge，`/metrics` 仍返回 200
  → 验证: 注入异常的用例断言 `/metrics` 状态码 200 且不含 5xx
- [x] 2.6 修正 `configs/prometheus.yml` scrape target，使其与 `docker-compose.yml` 服务名一致（D4）
  → 验证: 守卫测试断言 host 名与 compose 服务名一致

## 3. 既有面板与告警修正（D6：只修引用，不重排）

- [x] 3.1 `configs/alerts.yml`：`QueueDepthHigh` 改用 `apibridge_buffer_pending` 并调整阈值语义注释；
  `CPUUsageHigh` / `MemoryUsageHigh` 表达式不变（依赖 2.1 修复后即可求值）
  → 验证: 守卫测试（1.2）对该文件零缺口；输出改动前后对照清单
- [x] 3.2 `configs/grafana-dashboard.json`：队列深度面板改 `apibridge_buffer_pending`，
  新增 MQ 就绪数面板（`apibridge_queue_depth`，标注"内网无 MQ 时不显示属预期"）
  → 验证: 守卫测试对该文件零缺口；JSON 可被解析且面板数正确

## 4. 新增指标端点（React 看板数据源）

- [x] 4.1 `GET /api/v1/metrics/tasks?period=` —— 按任务维度聚合（成功/失败/最后运行/平均耗时/搬运行数）
  → 验证: TestClient 用例覆盖「时间窗口生效」与「无执行记录任务不报错」
- [x] 4.2 `GET /api/v1/metrics/connections?period=` —— 经 `field_mappings` 关联推导（D5），
  source/target 两侧分别统计
  → 验证: 用例断言同一映射两端各计一次、side 正确
- [x] 4.3 `GET /api/v1/metrics/errors?period=&limit=` —— 归一化错误文本分组 Top N，
  每项含 count、last_seen、≤50 条原样本
  → 验证: 用例覆盖 Top N 排序、limit 生效、样本上限 50
- [x] 4.4 `GET /api/v1/metrics/tasks/{task_id}/runs?limit=` —— 最近 N 次执行明细，任务不存在 404
  → 验证: 用例覆盖正常与 404 分支
- [x] 4.5 `app/schemas.py` 补响应模型；复核响应脱敏（`error_message` 不得回显凭证/`enc:` 前缀）
  → 验证: 用例断言响应体中不含 `enc:` 与 token 字样
- [x] 4.6 高基数防护：确认 per-task/per-connection 维度**只出现在**上述 REST 端点，
  未新增 Prometheus 标签（D7）
  → 验证: 守卫测试断言 `/metrics` 中不存在 task_id/connection_id 标签

## 5. 前端看板增强

- [x] 5.1 `frontend/src/pages/dashboard/index.tsx` 增加时间范围 + 任务/连接筛选器并联动全部图表
  → 验证: 清 `tsconfig.tsbuildinfo` 后 `npx tsc --noEmit` 零错误 + 浏览器实测联动
  （`scripts/monitoring_ui_check.py`：「选择任务后表格被过滤 2 → 1 行」PASS）
- [x] 5.2 新增「任务健康度」表格（成功/失败/最后运行/平均耗时/搬运行数），失败行标红
  → 验证: 浏览器实测（构造成功与失败任务各一）
  （UI 检查：「任务表有数据行 2 行」「失败任务行已标红 1 行」PASS）
- [x] 5.3 新增「错误 Top N」列表，点击跳转日志页并携带过滤条件
  → 验证: 浏览器实测跳转后日志页过滤条件与错误项一致
  （UI 检查：URL 含 `status=failed` + `start_time`/`end_time`，日志页过滤生效 PASS）
- [x] 5.4 行点击下钻抽屉（任务最近执行明细 + 错误项）
  → 验证: 浏览器实测（项目规矩：tsc 绿 ≠ 页面能用）
  （UI 检查：抽屉打开、10 行执行明细、标题「任务执行明细」PASS）

## 6. 回归与收尾

- [x] 6.1 全量 `python -m pytest tests/ -q` 通过（基线 568 passed / 11 skipped）
  → 验证: **794 passed / 13 skipped / 0 failed**（首轮 1 个性能预算用例因同机跑着
  uvicorn+vite 的负载干扰偶发失败，单独复跑与全量复跑均全绿）
- [x] 6.2 前端清缓存 `npx tsc --noEmit` 零错误 + 浏览器走查看板全功能
  → 验证: 清 `tsconfig.tsbuildinfo` 后 tsc 零错误；`scripts/monitoring_ui_check.py`
  15 项断言全过（截图存 `.tmp_ui/`，走查后已清理）
- [x] 6.3 更新 `docs/project-status.md`：记录指标链路修复项与新增端点，附可复现验证方式
  → 验证: 文档描述与实测输出一致（项目规矩：只写实测事实）
- [ ] 6.4 （可选，视本机 Docker 可用性）`docker-compose up postgres redis rabbitmq backend prometheus grafana`
  实测 Grafana 面板非空、`QueueDepthHigh` 之外三条告警表达式可求值
  → 验证: Grafana 面板查询返回非空序列 / Prometheus 表达式求值结果非 NaN
  → **结果：按任务自身条件跳过**（本机 Docker 仅 WSL 内可用，未拉起全栈；
  告警引用指标的真实存在性已由 1.2 守卫测试覆盖，全栈联测留作部署验收）
