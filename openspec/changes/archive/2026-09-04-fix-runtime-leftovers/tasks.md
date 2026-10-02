## 1. 本轮收口（已完成）

- [x] 1.1 看板接口跨库：统计窗口改 datetime 边界、分位数 Python 计算、趋势 Python 分桶 → 验证: SQLite 下 overview/trend 均 200（test_dashboard_sqlite），test_e2e 指标断言适配插值语义
- [x] 1.2 幂等键生命周期：is_duplicate 改只读 GET，新增 mark_processed 在推送成功后占位 → 验证: 失败未占位重试可重跑（test_failure_leaves_no_key_so_retry_reruns）；sync/async 双路径接入
- [x] 1.3 testing 路由前缀修正：去掉叠加的 `/api/testing`（改 `/testing`），export/import 移到参数路由前 → 验证: /api/v1/testing/cases/export 200 且与前端调用路径一致（test_export_route_reachable、test_no_double_prefix）
- [x] 1.4 LogResponse 补 from_attributes（真实 DB 日志查询 500 修复，冒烟中发现）

## 2. 待办（需独立设计，不夹带）

- [x] 2.1 连接池空闲回收/TTL（全局注册表常驻；低优先级）（实现: sweep_idle_pools 按 last_used 回收超时池 + lifespan 循环；test_pool_sweep 2 项）
- [x] 2.2 增量同步（incremental_field 只有字段没有实现）（实现+实测: 四连接器 where_gt + 端到端两次运行只搬新增、last_sync_value=7；并统一列表源转换语义 transform_payload（修复 DB 列表源映射产出 null 的既有缺陷））
- [x] 2.3 定时同步（schedule_cron 只有字段没有实现，需 Celery Beat 接线）（实现+实测: scheduler.py croniter 解析/到期派发/无效推迟；smoke_runtime_features 场景 3 真实派发成功、last_run_at 更新（补上 sync 流程缺失的 last_run_at 维护））
- [x] 2.4 Webhook 接收端（webhook_config 只有字段没有实现）（实现: POST /api/v1/webhook/{task_id}——secret 校验、sync 直跑/async 投递双模式；test_webhook 3 项）
- [x] 2.5 push_data SQLite REPLACE 行数语义核对（实测: SQLite INSERT OR REPLACE cursor.rowcount=1 语义正确，连接器逐行累计即真实行数，无需修改）
