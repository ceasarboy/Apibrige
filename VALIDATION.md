# 验证记录（整合版：每轮一节，按时间顺序）

## 打包验证（2026-09-29）

- 来源：干净工作区，提交 d10f2ec；完整提交、运行时和依赖摘要见 BUILD.json。
- 包内 Python 3.12.10 执行后端全量测试：1042 passed、11 skipped。真实 Oracle 环境用例跳过。
- 前端：npm run build、tsc --noEmit、14 项 UI 检查通过（scripts/multi_board_ui_check 类脚本）；Vite 有非阻断的大文件提示。
- 新建测试库 + 独立端口验证首启、首页、SPA 子路由、健康检查、Token 鉴权、监控接口、重复启动拒绝；启停与端口释放验证。
- 合成测试库重复执行迁移，启用 BI：状态/指标/看板 API 及 /bi、/bi/screen 页面正常。
- 包内 Python 及 VC++ 安装程序数字签名有效；Oracle Instant Client 19.32 预检通过。
- 本包在构建电脑验证，未在另一台物理机/真实 Oracle/明道云系统执行同步。

## 合并后基线（2026-09-30，两线合并 + 浏览器全功能实测）

- 路由并集 99/100（1 项有意移除）；包内 `runtime\python.exe -m pytest tests/ -q` = 1078 passed / 13 skipped（测试自足：conftest 临时 SQLite + 测试密钥）。
- 前端 `tsc --noEmit` + `vite build` → `web/`；升级迁移对既有库建齐 5 张 BI 表（含 bi_access_profiles）且存量数据完好。
- 运行时冒烟 + P6 只读令牌链路 + 浏览器全功能实测（源/目标 SQLite 真实搬运、BI 数值逐项核对、全程零控制台错误）。

## 二轮：全面代码审查修复后（2026-09-30）

- 1118 → 见 MERGE-REPORT §3.1；浏览器抽检：连接页新建弹窗标题正确、生产看板 17 卡渲染与数值对照一致。

## 三轮：第二轮审查修复后（2026-10-01）

- **1118 passed / 13 skipped / 0 failed**（+21 回归：tests/test_review_round2.py 等）；前端 npm run build 重建 web/。
- 修复范围 1 P0 + 10 P1 + 10 已确认 P2（明细 MERGE-REPORT §3.2）。
- 已知待真机回归：HAP `_stable_scan` 排序参数（见六轮——已回归并还原）。

## 四轮：剩余清单全量修复后（2026-10-01）

- **1136 passed / 13 skipped / 0 failed**（+18：tests/test_review_round3.py）；前端 npm run build 重建 web/。
- 修复范围 28 项（明细 MERGE-REPORT §3.3）。

## 五轮：PostgreSQL 部署级复现（2026-10-01，本机 PG 15.5 实测）

- **§3.3-49（UtcTimestamp）在真实 PostgreSQL 上验证通过，"待真机复现"项关闭。**
- 失败模式实锤：原生 asyncpg 对 TIMESTAMP WITHOUT TIME ZONE 绑定 aware datetime 至今抛 DataError（naive/aware 相减）。
- 修复生效：35 个时间列 create_all；ApiConnection 全默认值 INSERT/SELECT（naive UTC 回读，时差 0.01s）；DataBuffer JSON 列 datetime/Decimal 经引擎 serializer；ProbeSession（+08 语义）；BI 桶/水位线。
- 真库跑测试：test_api + test_bi_api **65 passed**；phase1 + security **52 passed**。
- 迁移：全新库首装 16 张表；既有库幂等重跑。脚本 `scripts/pg_live_check.py` 可重跑；临时库已删。

## 六轮：HAP sortType 真实端点回归（2026-10-01，api.mingdao.com 只读实测）

- **结论：审查 §3.2-34 为误报——原代码 `isAsc: true` 正确，已还原（merge_ws 14c6f68）。**
- 参数矩阵（scripts/hap_sorttype_check.py）：`isAsc` 是真实方向参数（true=升序/false=降序，rowid 字典序单调性证实）；`sortType` 的值被忽略（asc/desc/2/ASC 同序，有 sortId 时默认降序）；同请求两次一致、第 1/2 页无重叠。
- P4 增量当年验证通过靠 `filters`（utime > 水位线），排序仅辅助——这也是误报长期未暴露的原因。

## 七轮：修复后复审（2026-10-01，4 路并行 diff 复审 + 全量修复）

- 复审范围：98c3f21..HEAD 全部修复 diff；结论=主体闭环扎实，发现 3 P1 + 若干 P2/P3（含两处此前修复自身引入的回归），**全部当轮修复**，明细 MERGE-REPORT §3.4。
- 套件：**1136+ passed / 0 failed**（含 round5 修正回归）；前端 npm run build 重建 web/。
