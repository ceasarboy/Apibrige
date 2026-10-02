# add-bi-sql-pushdown 任务清单

## 1. 驱动层 fetch_grouped

- [x] 构造器 `_build_grouped_sql`：**SQLAlchemy 表达式编译**（阶段一，4b7a6dc）
      ——标识符白名单、值参数化（qmark + positiontup 有序参数）、列/IN(OR 元组)/BETWEEN/
      排序表达式/分页；别名 a0..an + alias_map；单测 10 项
- [x] 执行点接入（阶段三，dd2e489）——受控例外已获用户放行（A 选项），环境层停用
      Mimosa 插件后落地；实现形态较清单有一处收敛：**三方言执行全部收进同一模块**
      （不分散到各连接器类），使"新增执行点 = 单一模块单一函数"成立：
      `app/connectors/grouped_pushdown.py::fetch_grouped_rows`
      （标识符白名单 + 全值绑定 + 表达式编译；`supports_group_pushdown` 能力标志可全局关；
      超限 GROUP_ROW_CAP=200k 与任何执行异常 → 回退）

## 2. 判定与接入

- [x] `bi_sources.can_pushdown(defn, conn)` 纯函数 + 回退原因 + 进程内判定缓存
      （`_pushdown_cache`，TTL 60s 同行缓存；失败原因一并缓存，避免每窗重试）
- [x] `compute_window` 下推分支（entries 归一与内存路径逐项一致——13 组差分对照）
- [x] 异常兜底（回退 + 标记缓存）：超限/源库报错/连接不存在均不上抛；
      连接与 source_ref 问题回退后由内存路径抛出同样的 SourceError（错误语义不变）
- [x] 单测：可/不可下推矩阵（p50/computed/groups/resolver/contains/empty/HAP/Oracle/
      中文列名/度量级 filter/ratio 分量）+ reason 断言 + 缓存命中不重复判定
- [x] 超出清单的等价性补强：**NULL 行等价补偿**——SQL 三值逻辑把 NULL 判为不匹配，
      而内存匹配器对 ne/not_in/not_between（及空串参与比较的 lt/le）返回 True；
      判定侧用**同一份内存匹配器**对 NULL 行求值，为真时给构造器 `null_match` 补
      `OR IS NULL`，两条路径逐行等价（差分对照含 NULL 行用例实测）

## 3. 上限与并发

- [x] DB 源下推路径不受 MAX_FORM_ROWS；HAP/回退维持 10 万闸 + 5 万提示
      （下推不下拉全量行 → 天然不受闸；判别证据见 §5）
- [x] 内存聚合按行数阈值（≥2000）走 `asyncio.to_thread`（小聚合零调度开销）
- [x] 在飞重查询信号量（≤4，循环感知代理）；投放读桶不受限
- [x] 并发实测（scripts/bi_pushdown_bench.py，100 万行）：下推聚合期间查询
      最大抖动 **81ms**（预算 500ms ✓，>1s 0 次）；明细首屏 4.4ms/深翻页 34.5ms

## 4. Oracle 存量修复

- [x] 逻辑键映射（大小写互认）——`bi_aggregator.get_field` 顶层键与 `data` 包装内键
      均按小写归一二次匹配（阶段二 45b064a）
- [x] 单测：大写元数据 + 小写键行 → 聚合正常（tests/test_bi_aggregator.py
      `test_get_field_case_insensitive_*`）

## 5. 验收与收口

- [x] 内存路径拉行批量调优（FORM_FETCH_BATCH=20000，零新增执行点，c056aa4）：
      百万行聚合首轮 **40.3s → 5.5s（7.3×）**；≥10 万行内亚秒级
- [x] 100 万行基准（下推路径，2026-09-29 实测）：聚合首轮 **590ms**（预算 3s ✓；
      次轮水位线短路 5ms）。**下推路径功能性判别**：接口合计 = 整表 SQL 合计
      298,499,591.33 且 ≠ 前 10 万行合计 29,850,028.35 —— 内存路径会截断在 10 万行，
      故等于整表即"确实走了下推"（比耗时更硬的证据）
- [x] 数值三组对照（bench [3a']，真值 = 源库 SQL 直查，相对误差 1e-9）：
      分组（line × sum(actual_qty)，5 组）/ 汇总（count+sum+sum+avg）/
      过滤（定义级 line='A线' × shift 分组，3 组）**全部 PASS**
- [x] 全量 pytest：**984 passed / 13 skipped**（基线 929 + 新增 55，零回归）
- [x] 三 UI 自审脚本零回归：生产看板 25 项 / 多看板 14 项 / 显示设置 15 项全过
      （生产看板 KPI 94.11/84.79/96.37/97.22 现由下推路径算出，与改造前基线一致）；
      另新增 scripts/field_label_ui_check.py 8 项（明细列别名渲染，见同期收口）
- [x] 文档（design §8 落地记录与偏差）+ 本 tasks + project-status 更新 + OpenSpec 归档

## 6. 残留（明确不在本期）

- 度量级 filter（条件聚合 = CASE WHEN）：判定即回退（proposal「不做」清单）
- Oracle 下推：能力标志默认 False，11g ROWNUM 双层 + 聚合/排序组合需单独验证
- 明细 keyword 搜索 LIKE 下推：仍走内存（源库 LIKE 的大小写/转义语义跨库不一致），
  大表上表现为"10 万行内过滤 + 明确 fallback 标记"
