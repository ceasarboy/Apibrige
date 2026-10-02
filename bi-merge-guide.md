# BI 与监控增强：启用和回退

本次基于当前 APIBridge 同步引擎增量集成，不替换 REST 契约/分页、HAP V2 批量写入、增量轮次、水位、失败恢复、探针审批或现有鉴权。

## 2026-09-30 合并新增（开发线 P1–P6 已并入本包）

在原有指标/图表/大屏之上，本包增加了以下能力（`MERGE-REPORT.md` 有完整清单与上线差异）：

- **明细大表逐行分析**：明细卡支持服务端分页/排序/搜索，DB 源分页下推；列可设显示别名。
- **计算字段与展示修饰**：行级派生列、聚合比、维度分组、占比/累计（仅作用于分析）。
- **即席分析工作台**（`/bi/analysis`）：拖拽字段即时出图，可另存为看板卡片。
- **聚合下推**：DB 源大表按维度下推聚合，执行点收敛在 `app/connectors/grouped_pushdown.py`；`supports_group_pushdown` 可全局关闭（Oracle 默认关）。
- **HAP 源增量拉取**：`utime` 水位线 + 两段式增量 + 定期全量对账；阈值/关闭开关见 `docs/bi-dashboard-guide.md` 的"HAP 增量拉取运维要点"。
- **看板数据权限**（`/bi/access`）：为指定看板签发**只读令牌**，可配看板级可见性 / 行过滤 / 列隐藏；令牌明文只在创建或轮换时出现一次，库内只存 sha256。
  使用方式：管理令牌建档案 → 拿到 `bit_…` 令牌 → 调用方以 `X-BI-Token: <令牌>` 访问 `/api/v1/bi/*`（无需管理令牌）；受限身份访问写端点一律 403。
- **启用方式不变**：仍由 `APIBRIDGE_BI_ENABLED` 与 `APIBRIDGE_BI_AGGREGATION_ENABLED` 控制；BI 关闭时 `/api/v1/bi/*` 全部 404，`/bi/status` 例外（前端据此隐藏入口）。
- **与合并前不同的默认值（务必看）**：webhook 任务的 `secret` 现在**默认强制**（未配 → 409）；调度器**空 `schedule_cron` 静默跳过**（不再推迟+告警）；BI 表单读取**超上限拒绝**而非截断；CORS 不再放开 `*`（跨机访问需设 `APIBRIDGE_CORS_ORIGINS`）。逐条说明见 `MERGE-REPORT.md` §8。

## 默认状态

- `APIBRIDGE_BI_ENABLED=false`：BI API 和页面不启用。
- `APIBRIDGE_BI_AGGREGATION_ENABLED=false`：不运行自动聚合。
- 新的健康度监控是现有监控页的增量扩展，旧总览/趋势 API 保留。
- 不含同步缓冲自动清理，不会由 BI 删除 `DataBuffer`、`SyncRound` 或 `HapWritePlan`（合并后仍成立：`purge_expired_buffers` 只作为可调用能力保留，不接入自动循环）。
- 本次实施不修改现有 `.env`，不升级或写入现有业务数据库，不开启真实业务任务。

## 升级已有环境

1. 等当前同步完成，停止 API 和 Worker；备份平台数据库以及与其配套的 `.env` 和加密密钥。便携环境还应备份 `launcher.json`。
2. 使用当前环境对应的 Python 和数据库配置执行 `python -m app.migrations`。Windows 项目虚拟环境可以用 `.venv\Scripts\python.exe -m app.migrations`；便携包沿用 `migrate.cmd`。
3. 迁移新增四张 BI 表和迁移版本记录；保留原同步表及所有数据。重复执行不会重复创建记录。新库随首次启用自动建立完整表结构。
4. 将 `APIBRIDGE_BI_ENABLED=true` 写入部署环境配置并重启，保持自动聚合关闭。
5. 登录后进入“BI 大屏”及指标定义页面，选择已注册的连接表单，配置指标，先手动执行聚合并核对数值。
6. 数值和布局确认后，再设置 `APIBRIDGE_BI_AGGREGATION_ENABLED=true`，重启开启自动聚合。

仅开启自动聚合开关、未开启 BI 功能时，不会启动 BI 调度。内置指标种子全部默认停用，不会在启动时读取外部业务源。

## 能力与边界

- 指标：求和、平均、计数、极值、去重计数、分位数、聚合比；行级计算字段、筛选、维度分组、占比及累计。
- 17 种图表：柱状、折线、时间坐标折线、饼图、散点、雷达、热力、3D 矩阵、桑基、关系、途径、树、漏斗、仪表盘、表格、指标卡、原始明细。
- 布局：72×36 网格，多看板，指标复用，拖动缩放，布局服务端保存，独立投放。
- 投放：`/bi/screen?board=<看板标识>`，四种适配方式、深浅主题、明细自动轮播。投放仍要求管理 Token，隐藏导航不等于公开访问。
- 表单源支持当前数据库连接器和 HAP V2 工厂适配器。没有切换为对方的 V3 元数据实现。
- 表单是当前状态快照，不支持历史回填，也不允许展示层累计。
- 加密缓冲批次只在内存解析；数组按实际记录展开，准备状态跳过，解密失败明确报错。
- 表单和窗口读取上限默认为 100000 行，结果超限拒绝计算/返回，避免把部分数据误报为完整结果。原始明细输出最多 500 行。
- 表单缓存有效期为 60 秒，默认最多 16 个缓存条目；连接或字段字典变更会改变缓存键。
- BI 请求默认 30 秒预算，最多两个请求同时执行；聚合与指标定义修改串行，锁与同步任务独立。
- 自定义 SQL 在独立数据库只读连接执行；SQLite 使用 query_only 和执行进度超时，PostgreSQL 使用只读事务与 statement_timeout。语句还须通过静态只读校验。
- BI 后台每 60 秒尝试聚合。Redis 不可用或锁被占用时跳过本轮，恢复后续跑，不降级为无锁聚合。部署沿用单 API worker 的约定。
- 指标定义更新后清除该指标的旧桶与 BI 水位，再按新定义计算；不修改同步水位。
- BI 计算字段只作用于分析，不改变已有同步字段转换规则。

可调配置：`APIBRIDGE_BI_TICK_INTERVAL_SECONDS`、`APIBRIDGE_BI_QUERY_TIMEOUT_SECONDS`、`APIBRIDGE_BI_MAX_ROWS`、`APIBRIDGE_BI_CACHE_ENTRIES`。扩大限额前应先评估数据量和内存。

## 监控口径

新任务健康表的成功/失败数是执行日志数量，缓冲批次数是 DataBuffer 数量，均不是业务行数。连接健康度从关联任务日志推导。原总览/趋势接口口径不变。

原 Prometheus 指标名和标签保留。新增 `apibridge_buffer_pending` 和 `apibridge_mq_queue_depth{queue_name=...}`；MQ 不可达时不输出该队列序列，不能解释为队列为零。刷新有缓存和时间预算，避免每次抓取重复等待 MQ。

日志下钻复用当前日志页，保留失败源记录分页和字段详情。

## 验证方式

- 后端：`.venv\Scripts\python.exe -m pytest tests/ -q`。
- 前端：在 frontend 执行 `npx tsc -b --force`、`node --test tests/*.test.mjs`、`npm run build`。
- 浏览器：构建后执行 `.venv\Scripts\python.exe scripts/verify_bi_ui.py`。验证环境需安装 Playwright 和本机 Edge；这不是生产依赖。
- 浏览器脚本使用临时合成数据库和临时端口，禁用同步调度及自动聚合；结束后关闭自己启动的服务。报告和截图在 `tmp/bi-ui-<run>/`。
- 当前业务库、真实 HAP/Oracle 连接不参与上述测试；外部真实写入仍须遵守探针和人工 L3 授权流程。

## 回退

先关闭两个 BI 开关并重启。需要代码回退时恢复之前应用版本，保留新增 BI 表和平台现有同步记录，旧版本可忽略新增表。不要为了普通应用回退直接恢复上线前的旧业务数据库，否则会丢失上线后同步进度。

## 便携部署

保持原打包流程。前端新依赖已记录在 package-lock.json，构建产物须重新生成并随便携包交付；Python 运行时无需新增生产依赖。已有便携数据目录使用 migrate.cmd 升级后再开启 BI。
