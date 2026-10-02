# APIBridge Windows 便携包

适用于 Windows 10/11 x64，建议 4GB 以上可用内存、2GB 以上磁盘空间。
包内含独立 Python 3.12、后端源码及依赖、编译前端、Redis Windows 和 Oracle Instant Client 19。
无需安装 Python、Node.js、Docker 或本地 Oracle 数据库服务端。解压与运行可离线完成；连接远程业务系统需要网络。

## 首次使用

1. 将整个 ZIP 解压到可写的英文目录，例如 `D:\APIBridge`，不要直接在压缩包内运行。Oracle 19 客户端不支持本次测试中的中文路径；路径可含空格，请勿放在带中文用户名的桌面目录。
2. 如果电脑没有 Microsoft Visual C++ 2015–2022/新版 x64 运行库，运行 `prerequisites\VC_redist.x64.exe` 安装。
3. 在 CMD 中进入解压目录，执行 `start.cmd`。子服务后台运行，日志保存在 `logs`。
4. 打开 http://127.0.0.1:3000 。用记事本打开解压目录里的 `.env`，复制 `APIBRIDGE_ADMIN_TOKEN=` 后的值，在页面右上角 Token 中保存。
5. 通过连接管理配置自己的源和目标。执行任务前确认目标表、主键和删除同步开关。

`stop.cmd` 停止本包启动的 API 和 Redis；`status.cmd` 查看状态。停止不会删除数据库或密钥。
强制关闭电脑前建议先执行 `stop.cmd`。API 停止会中断正在运行的同步，请等待任务结束后停止。

## 数据和配置

- 首次启动自动生成随机管理令牌、加密密钥、Redis 密码和全新的 `data\apibridge.db`。
- 本包不包含打包电脑的 `.env`、SQLite 数据库、连接账号、映射、任务、探针样本和日志。
- 数据备份：停止服务后备份 `.env`、`launcher.json` 和整个 `data` 目录。恢复时必须同时保留原加密密钥。
- 配置只读取本目录 `.env`，避免被目标电脑已有的 APIBRIDGE 环境变量覆盖。
- 默认仅本机访问；Web 端口是 3000，Redis 是 16379。端口冲突时停止服务，修改 `launcher.json`；改 Redis 端口时还要同步修改 `.env` 的 `APIBRIDGE_REDIS_URL`。
- 不要把已运行、已填入真实配置的目录当作干净安装包转发。

## Oracle 远程连接

默认开启 thick 模式，客户端路径相对本目录解析，兼容 Oracle 11g。
连接远程数据库无需安装本地 Oracle 服务端。在页面配置远程 IP、端口、SID 或支持的服务名配置及账号。
若只连接 Oracle 12.1+，可以将 `.env` 的 `APIBRIDGE_ORACLE_CLIENT_MODE` 改为 `thin`。
探针访问内网地址还需要在 `.env` 的 `APIBRIDGE_PROBE_ALLOWED_HOSTS` 中添加管理员允许的主机，逗号分隔，重启生效。

## 当前版本（2026-09-30：合并 GPOST 开发线）

本包**已与开发线（GPOST）合并**：在本文发行包基线之上移植了 BI 大演进 P1–P6，并修复合并中暴露的若干问题。合并的完整过程、逐文件冲突解决、验证结果与上线差异见 **`MERGE-REPORT.md`**。

新增能力（开发线移植）：

- **明细大表逐行分析**：卡片分页/排序/搜索，DB 源分页下推不拉全量；列显示别名。
- **计算字段与展示修饰**：行级派生列、聚合比、维度分组、占比/累计。
- **即席分析工作台**（`/bi/analysis`）：字段树 + 拖拽槽 + 即时出图 + 另存卡片。
- **聚合下推**：DB 源大表按维度下推聚合（`app/connectors/grouped_pushdown.py` 单一执行点，能力标志可关）。
- **HAP 源增量拉取**：`utime` 水位线 + 两段式增量 + 定期全量对账（阈值与关闭开关见 `docs/bi-dashboard-guide.md`）。
- **看板数据权限**（`/bi/access`）：只读访问令牌（`X-BI-Token`）+ 看板级可见性 / 行过滤 / 列隐藏，令牌只存哈希。
- 后端与前端工具链：本包内自带测试（`tests/`，1012 项）、规格基线（`openspec/`）、开发文档（`docs/`）与运维脚本（`scripts/`）。

**上线前请阅读 `MERGE-REPORT.md` §8**：webhook secret 默认强制、BI 端点启用门禁、CORS 默认值收紧、表单读取超上限改为拒绝等行为差异。

## 当前发行基线（2026-09-29）

发行包自身基线基于提交 d10f2ec，包含 BI 大屏、监控增强及探针无时区时间按 UTC+08:00 解析的修复。同步能力包括：明道云批量 upsert、批次源数据刷新、手动增量水位、失败行和字段详情，以及长时间同步请求的等待修正。功能说明分别见 hap-bulk-upsert.md、manual-watermark.md、execution-log-rules.md 和 incremental-sync.md。

## 执行模式

本版包含增量字段配置与整轮提交水位。已有数据目录升级前，停止所有服务并备份 `.env`、`data`，然后运行 `migrate.cmd`。新目录首次启动无需迁移。增量源要求非空数字/日期时间字段和源表主键；数据库目标要求主键 upsert。任务详情提供“增量进度”和“重试未完成批次”。完整说明见 `incremental-sync.md`。

本包开箱提供本地同步执行（`sync`）和 Redis；RabbitMQ/Celery Worker 没有随启停脚本启动。
任务请选择 `sync`。`async` 和触发异步分支的 `auto` 需要另行部署 RabbitMQ 与 Worker，建议在 Linux 上部署 Worker。
自动 Cron 调度默认关闭；确认任务配置后，将 `.env` 的 `APIBRIDGE_SCHEDULER_ENABLED` 改为 `true` 再重启。
现有同步/异步能力差异仍以项目实现为准，包不会自动调整业务任务模式或同步数据。

## 内容与验证

`app/` 为当前后端代码；`frontend-source/` 为前端源代码；`web/` 为编译产物；`skills/` 为探针 CLI 与 BI 配置说明。
`requirements.lock` 固定本包的 Python 依赖版本；`MANIFEST.sha256` 记录发行文件的 SHA256。
`VALIDATION.md` 记录打包验证结果和限制。`THIRD_PARTY.md` 提供第三方组件来源。`MERGE-REPORT.md` 记录与开发线合并的完整过程与差异。
开发/验证资产：`tests/`（pytest，运行 `runtime\python.exe -m pytest tests/ -q`，无需环境变量）、`openspec/`（能力域规格基线）、`docs/`（设计与使用说明）、`scripts/`（冒烟与自审脚本）、`configs/`（Prometheus/Grafana 示例）。
便携包面向单机内网使用；Redis Windows 移植项目定位为开发测试用途，正式高可用部署请使用 Linux 服务。


## 可选 BI 与监控增强（2026-09-28）

本版增加指标计算、17 种图表、多看板、大屏投放与运行健康度。原同步和失败恢复流程保持现有实现。

BI 与自动聚合默认关闭。已有数据目录先停止服务、备份并执行 migrate.cmd，再按 bi-merge-guide.md 开启；不要替换或恢复为开发机器的数据库。BI 不清理同步缓冲。关闭 BI 开关即可停用分析功能，保留已有同步数据。

## 从旧便携包升级

1. 等待同步结束，运行旧目录 stop.cmd。
2. 备份旧目录 .env、launcher.json 和整个 data 文件夹。
3. 新 ZIP 解压到另一个英文目录，将上述文件复制到新目录。
4. 运行 migrate.cmd，成功后运行 start.cmd。
5. 确认原连接、映射、任务和运行情况正常；保留升级前备份。需要 BI 时按 bi-merge-guide.md 启用。

不要用新生成的密钥覆盖旧 .env；否则已有凭据可能无法解密。
