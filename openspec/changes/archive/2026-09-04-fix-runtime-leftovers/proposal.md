## Why

`add-agent-bridge-authoring` 完成后，上一轮评审记录的遗留问题仍未修，需另立 change 收口（不在本变更范围）。

## 遗留清单（实测证据见 docs/project-status.md 基线）

1. 监控看板接口在默认 SQLite 下 500（PG 专属 `interval` 与 `percentile_cont`），`app/api/metrics.py`
2. 幂等键在推送成功前即被 SETNX 消费且从不回收 → 失败重试被静默当重复跳过；`fallback_to_async` 空转，`app/cache/idempotency.py` + `sync_handler`
3. 连接池无空闲回收/TTL（全局注册表常驻），`app/connectors/database.py`
4. `push_data` 个别分支仍返回 `len(data_list)` 而非驱动 rowcount（SQLite REPLACE 行数语义需核对）
5. 增量/Cron/Webhook 三个 P0 特性只有字段没有实现
6. 路由前缀叠加 `/api/v1/api/testing/...` 与 `GET /cases/export` 被 `/cases/{id}` 抢匹配，`app/api/testing.py`

## What Changes

- 每项独立小任务，按 P0 排序逐个修复并补测试（详见 tasks.md，待立定时生成）
- init.sql 一致性检查纳入 CI（已完成：gen_init_sql.py --check）
