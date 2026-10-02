# 合并报告：发行包主线 ← GPOST 开发线特性

日期：2026-09-30
目标（主干基线）：`E:\APIBridge-Windows-x64`（发行包，构建于提交 `d10f2ec`）
移植来源：`E:\GPOST`（开发线，HEAD `0c33167`）
工作区（三方合并与历史留档）：`E:\_merge_ws`（git 仓库）
包外备份（合并前的目标包 app/frontend-source/web/skills）：`E:\APIBridge-pre-merge-backup-20260930`

---

## 1. 两条线的关系（为什么不是"谁覆盖谁"）

两边**没有共同 git 提交**（目标包声明基于 `d10f2ec`，该提交不在 GPOST 历史里），但内容可证同源：
按"逐提交内容比对"定位到**共同祖先 ≈ GPOST 提交 `9310c9b`（2026-09-27，BI 计算字段后端）**——
目标包在该点的 84 个同名文件中 45 个与 GPOST 逐字节相同，其余为各自后续演进。

| 侧 | 相对祖先的主要演进 |
|----|------------------|
| 目标（发行包 / d10f2ec 线） | 增量轮次（round/lease/重试）、HAP 批量 upsert、手动水位、监控增强（monitoring.py）、字段策略重构（field_dict → field_policy）、rest 契约与分页、适配器错误模型、探针时间戳约定（无时区=UTC+08:00）、BI 运行时预算（BiRoute/loop_lock）、CORS 放宽、webhook secret 可选 |
| GPOST（开发线） | BI 大演进 P1–P6：明细大表逐行分析、计算字段/聚合比、字段显示别名、即席分析、聚合下推（P3）、HAP 源增量拉取（P4）、看板数据权限（P6）；字段字典端点、CORS 收紧、路由模板化指标标签、webhook 豁免+强制 secret、聚合下推受控例外 |

分类（84 个同名文件）：**44 两边相同 / 1 仅 GPOST 改 / 31 仅目标改 / 8 双方都改**；
另有 4 个文件仅 GPOST 有（`grouped_pushdown.py`、`bi_access.py`、`bi_adhoc.py`、`field_dict.py`）、
24 个仅目标有（增量轮次、hap_bulk、field_policy、bi_runtime 等）。

**合并原则**：以发行包为主干；行为默认值以主干为准，**能力与新功能以 GPOST 为准**；
双方语义冲突处按"更安全/信息更全"的一侧并显式记录（见 §4、§5）。

---

## 2. 合并方式（可复核）

在 `E:\_merge_ws` 内做**真三方合并**：

1. `git checkout -b target-side 9310c9b`，把目标包的 `app/`、`frontend-source/`、`skills/` 作为一次提交（`7b9c348`）；
2. 从 GPOST `main` 分支出 `merged` 合并 `target-side` → 11 个冲突（`902cd6f`）；
3. 逐一按语义解决（下表），再按合并后实现适配测试、修复暴露的真问题（`77371ba`、`e3a9985`）。

冲突与解决：

| 文件 | 双方改动 | 解决 |
|------|---------|------|
| `app/main.py` | 我方：CORS 收紧/路由模板标签/webhook 豁免/BI 令牌放行；目标：迁移式 lifespan、BI 启用门禁、`refresh_cached` | 取目标 lifespan + 我方中间件与 CORS 块（CORS 最后注册以包住两个 http 中间件） |
| `app/config.py` | 目标：`bi_enabled`/incremental_*；我方：`cors_origins` | 目标配置 + `cors_origins` + 清理由 purge 参数（保留能力，见 §5） |
| `app/api/bi.py` | 我方：P1/P2/P6 全量路由 + 身份层；目标：`require_bi` 门禁、BiRoute 预算、form-rows 脱敏与错误收敛 | 我方路由为主体 + 目标的门禁/预算/脱敏；`_load_form_field_meta` 改走目标方 `get_connection_fields`（旧 `field_dict` 已删除） |
| `app/services/bi_sources.py` | 我方：批量分页（20k/批）、HAP 增量、下推判定；目标：行上限+超时、字段字典成形+脱敏、错误脱敏 | 目标语义为主体 + 我方批量分页与 HAP 增量；超上限**拒绝**而非截断（不把部分数据当完整结果） |
| `app/services/bi_aggregator.py` | 我方：下推 + 并发硬化（信号量/阈值）；目标：纯 scan_window | 取我方（目标为子集） |
| `app/connectors/database.py` | 我方：`supports_group_pushdown`；目标：工厂内解密 | 目标解密 + 我方能力标志 |
| `app/adapters/hap.py` | 我方：`get_rows_since`/sort/return_total/业务键读行；目标：V2 单链路重构 + 批量 upsert + normalize | 目标实现为主体 + 补回我方增量接口与业务键读行 |
| `app/services/bi_scheduler.py` | 我方：明细保留清理 + 无锁降级；目标：硬化循环（超时/序列化/失败隔离）+ 不清理缓冲 | 目标循环 + 我方 `purge_expired_buffers` 保留为**可调用能力**（不接入循环，见 §5） |
| `app/probe/session.py` | 我方：`_as_utc` 暂存比较；目标：`as_east_eight` | 取目标（无时区=UTC+08:00 为全项目统一约定，见 `app/probe/timestamps.py`） |
| `app/schemas.py` | 我方：新增 `BiAdhocQueryIn`；目标：BI/监控 DTO 拆到 `bi_schemas.py` | 目标结构 + `BiAdhocQueryIn` 归入 `bi_schemas.py` |
| 前端 `App.tsx` / `BiBoard.tsx` | 我方：分析页/授权页路由、明细查询 UI；目标：lazy 加载 + `BiGate`、`nextFreeCell` 重写 | 目标结构（lazy+BiGate）承载我方路由；明细表取我方 UI + 保留目标 `data-bi-detail-scroll` 钩子 |
| `skills/bi-dashboard/SKILL.md` | 目标删除、我方修改 | 保留我方 |

---

## 3. 合并中修出的真问题（代码改动，非测试放宽）

1. **HAP 目标删除对账的键兼容**（`app/services/hap_target.py`、`app/services/sync_handler.py`）
   目标方把开关键从 `delete_sync` 改为 `hap_delete_sync`，既有映射（GPOST 侧创建）的 `delete_sync` 不再触发删差 → 删除同步会**静默失效**。修法：HAP 目标下两键同等生效；同时把"判定是否 HAP 策略"与"目标是否 HAP"解耦，避免 DB 目标的 `delete_sync` 被误判进 HAP 策略校验。
2. **`rows` 包装键丢失**（`app/services/transformer.py`）
   目标方重写时 `_WRAPPER_KEYS` 少了 `rows`（MES `ListResponse` 顶层包装键），且遗留(v1)映射的缺失字段默认改为写 `null`——upsert 会用 null 清空目标已有值（此前的审查整改点）。修法：补回 `rows`；v1 默认改回 `skip`（不产出目标键），`null`/`error`/`default` 仍可按字段显式声明，v2 默认仍是 `error`。
3. **创建期枚举校验丢失**（`app/schemas.py`）
   任务/映射的 `sync_mode`、`task_type`、`conflict_strategy` 校验在目标线被移除，非法值会一路建到库里。修法：补回 `TaskValidation`/`MappingValidation` 校验（422 早失败）。
4. **webhook 变成无鉴权写入入口**（`app/api/webhook.py`、`app/config.py`）
   我方"端点豁免管理令牌"＋目标方"secret 可选"组合后，未配 secret 的任务可被任意调用触发同步。修法：`APIBRIDGE_WEBHOOK_REQUIRE_SECRET` 默认 **true**（未配 secret → 409 `webhook_secret_required`），保留显式放开开关。
5. **队列指标改名后旧名仅存桩**（`app/metrics_collector.py`、`app/services/metrics_refresh.py`）
   目标方把 `apibridge_queue_depth` 改名 `apibridge_mq_queue_depth{queue_name}`，旧名保留定义但不再赋值（既有看板/告警会看到空序列）。修法：同一份深度**同值双写**两个指标名，MQ 不可达时两者都移除序列（不谎报 0）。
6. **调度器把两种 cron 语义合成一种**（`app/services/scheduler.py`）
   目标方对"空 cron"也走"推迟 5 分钟 + 告警"，导致永久写库+刷日志，且 UI 显示一个永远不会发生的下次运行时间。修法：恢复区分——**空 cron 静默跳过**（不动 `next_run_at`），**非空但无效**才推迟并告警。

**浏览器全功能实测又抓到 2 处（2026-09-30 当天修复）**：

7. **投放链接打不开（合并引入）**（`frontend-source/src/main.tsx`、`src/api/client.ts`）
   目标方把 `/bi/screen` 包进了 `BiGate`，而"导入 `?token=bit_…` 并抹掉地址栏参数"的逻辑写在**投放页的 effect** 里——门禁先请求 `/bi/status`（此时未鉴权）→ 失败 → 投放页永不挂载 → effect 永不执行，形成死锁（现象：地址栏令牌不消失、页面提示"未配置管理 Token"）。修法：新增 `client.bootstrapBiTokenFromUrl()`，在 `main.tsx` **首个请求之前**导入令牌并清理 URL。修复后实测：令牌入 localStorage、地址栏干净、受限身份直接进屏幕页。
8. **受限身份读不到 `/bi/status`（P6 既有缺口）**（`app/services/bi_access.py`）
   只读白名单漏了 `GET /api/v1/bi/status`，受限令牌调用被 403（`readonly_token`）→ BiGate 卡在"无法读取 BI 状态"。该端点只回启用开关，无敏感信息。修法：加入 `READONLY_BI_ENDPOINTS`，并在 `tests/test_bi_access_scope.py` 补断言。

另有 1 处**可用性缺口（未改代码，留待决策）**：`bi-merge-guide.md` 要求"先手动执行聚合并核对数值"，但管理界面**没有手动聚合入口**（只有 `POST /api/v1/bi/aggregate/run` 接口与 60s 自动调度）。建议在指标定义页加一个"立即聚合"按钮；本期先用接口或开自动聚合。

**用户实测（看板"刷新"报错）又抓到 2 处前端问题（2026-09-30 当天修复）**：

9. **筛选栏候选值把大表读爆（502 `BI 数据读取失败，请检查数据源与配置`）**（`frontend-source/src/pages/bi/BiBoard.tsx`、`api/client.ts`）
   两处叠加：① 候选值加载器 `loadFormRows` 用的是**不带 page** 的旧路径（=全量读再截断），遇到百万行表直接触读取上限 → 502；
   ② 同一维度名在多张卡来自不同源表时，`collectDimDefs` 取"第一个"——按 key 排序时百万行压测表排在前面，于是"产线"候选取到了 L0–L4（错表）。
   修法：候选值改用**分页端点**（`page=1&page_size=200`，DB 源分页下推、只取一页）并标记为静默请求（失败不弹错）；候选表改为**按引用次数多数表决**（产线 → production_output ✓）。
   保留后端旧契约不动（`不带 page` 仍是"全量读+截断"，有测试断言）；文档标注"大表一律带 page"。
10. **筛选下发给"没有该维度"的卡 → 整卡被清空**（`BiBoard.tsx` 的注入逻辑）
    前端原先对**所有有分组的卡**下发全部 `dim=`，于是「车间」（自定义分组）/「日期」/「类型」这些维度名不同的卡被后端按不匹配的维度名筛空。
    修法：只下发**该卡自己拥有的维度名**；无维度卡（KPI/仪表盘）本就不下发。
    **已知口径（非缺陷）**：KPI 卡不随"产线"筛选变化（桶里没有该维度，需重算才可行）——如需要"按产线过滤的 KPI"，属新需求（可走即席查询路径或让查询端点支持无维度指标的重算）。

另有一处**合并一致性修复**：`app/api/bi.py::_load_form_field_meta` 原依赖已删除的 `services/field_dict.py`，改为经 `get_connection_fields`（字段策略的统一取数），否则 BI 字段树在合并后直接 ImportError。

### 3.1 全面代码审查修复（2026-09-30 第二轮，5 P0 + 13 P1 + 6 P2）

六路并行审查（安全/同步引擎/BI/API/前端/测试部署）发现的问题，逐项修复并带回归测试（全套 `1097 passed / 13 skipped`）：

**P0（数据正确性/越权）**

11. **webhook 推数 × 删除对账 = 清空目标表**（`app/api/webhook.py`、`app/services/sync_handler.py`）
    webhook 注入的单条会被 sync_transfer 当"完整源集合"，`delete_sync` 开启时删差把目标其余行全删。修法：webhook 层对删除对账任务直接 422 拒绝；`sync_transfer` 对注入源跳过删除阶段（双键 `delete_sync`/`hap_delete_sync` 同判）。
12. **HAP append 写模式 × 删差 = 重复行放大**（`app/services/hap_target.py`）
    append 模式不去重，第 2 轮全量重复写入后 `hap_key_duplicate` 永久失败。修法：开启删差时 push 阶段强制按业务键去重（`hap_target.push`）；append 写模式与删差的组合在策略校验中被拒绝。~~原记法 `hap_delete_write_mode` 错误码不存在~~（五轮复审文档修正）。
13. **受限令牌经 adhoc `computed` 表达式绕过列隐藏**（`app/services/bi_access.py`）
    `[隐藏字段]` 借派生列引用即可绕过 `adhoc_hidden_field_used`。修法：computed 表达式的 `extract_refs` 逐个对隐藏集校验。
14. **BI 聚合下推绕过字段可读性与脱敏**（`app/services/bi_sources.py`、`app/security/desensitize.py`）
    下推 SQL 直读源表列，不查字段字典 readable、不脱敏。修法：`can_pushdown` 增加敏感列门禁（命中脱敏提示词 → 不可下推，含 `app_key` 等分隔符变体的键归一化匹配）；执行前查字段字典，任一触达列 `readable=False` → 回退内存路径（fail-closed）。
15. **连接页"编辑保存后新建"静默覆盖旧连接**（`frontend-source/src/pages/connections/index.tsx`）
    保存路径漏 `setEditingId(null)`。修法：保存成功复位 + 新建按钮打开前清残留态（浏览器实测复现→验证）。

**P1（防线接线/逻辑正确性）**

16. probe_guard SSRF 防线原是死代码 → 接线进探针路径（`prober._api_adapter` + probes API 三处错误映射），总开关 `APIBRIDGE_PROBE_SSRF_ENFORCE` **默认关**（本地部署探针目标常在内网，与运行时同 URL；防线此前从未生效，默认关 = 既有部署行为），开启后白名单 `APIBRIDGE_PROBE_ALLOWED_HOSTS` 豁免。
17. BI 授权看板"一张卡都没摆"时 fail-open → 改 fail-closed（`bi.py::_ensure_metric_visible`，placed 空 = 什么都不可见）。
18. 合并回归：router 丢失 GPOST 的 async/auto × delete_sync 拒绝守卫 → 补回（双键）。
19. `hap_bulk` 计划机只认 `hap_delete_sync` 不认 legacy `delete_sync` → 双键同判。
20. `sync_handler` 源稳定性双读只认新键、`'source_data' in dir()` 恒真 → 双键 + 真 fallback 判定。
21. metrics 标签带完整 base_url（查询串鉴权会泄进 /metrics）→ 只留 `scheme://host:port`。
22. `_hap_snapshots` 无上限 → 200 上限淘汰。
23. `PUT /bi/screen-settings` 形状护栏（name/cells/filter_dims 结构校验，坏数据 422 不落库——此前整字段覆盖曾毁掉默认看板）。
24. BI 定义校验：维度缺 `name` 拒绝；度量 key 撞维度名拒绝（展平成行时度量会覆盖维度值）。
25. `_apply_top_n` 保用户排序（带 sort 时 Top N 只截断不改序；无 sort 保持主度量降序——看板排行卡不受影响）。
26. 前端批：BI 定义页/授权页编辑弹窗补 `resetFields`（filters/style/note 残留泄漏）；投放链接令牌按档案 key 绑定（跨行错拼令牌）；投放页 Esc 弹层打开时不再整页退出；4 个 zustand store 补 try/finally（失败永久转圈）；联调报告 iframe 加 `sandbox=""`。

**P2（加固）**

27. 连接测试/表结构错误信息过 `redact_text`（保留可诊断性，掩掉连接串中的凭证键值）。
28. logs `start_time/end_time` ISO 解析校验（422 早失败）；`TaskUpdate.auto_threshold` 补 `ge=1, le=100000` 边界（与创建同）；连接 `protocol`（适配器注册表 ∪ 数据库注册表）与 `connection_type` 枚举校验；SQLite upsert 由 `INSERT OR REPLACE`（删+插，partial upsert 清列）改 `ON CONFLICT DO UPDATE`（replace 模式语义不变）。

**决策留档**：增量水位严格 `>` 的同秒迟到行丢失属语义变更（需重叠窗口 + 幂等重放），本期不动，已记待办；docker-compose 不注 `ADMIN_TOKEN`（manage.py 首启随机生成，包本身安全）。

### 3.2 第三轮审查修复（2026-10-01，1 P0 + 10 P1 + 10 已确认 P2）

六路并行复审（当前状态、不与 §3.1 重复）发现的问题，按用户指令修复 P0+P1 全部与已确认（✔实测复现）的 P2；**其余 P2/P3 待用户拍板**（清单见审查报告）。

**P0**

29. **DB 目标删除对账缺"完整源集合"守卫**：`validate_policy` 的范围检查全在 HAP 目标分支，DB 目标 + 仅 `delete_sync` 早退放行 → HAP 过滤/视图源只回子集，DB 删差把子集外目标行全删。修法：范围检查提到 `validate_policy` 顶部对任何目标生效（`delete_sync_filtered_source`，v1/v2 配置形状都覆盖）+ `sync_handler` DB 删差分支加跳过守卫（纵深防御）。

**P1**

30. **失败批次 data_hash 毒化冲突检测**：hash 在推送前落库，冲突比对不过滤 status → 失败重试被永久判"已存在"跳过。修法：抽 `_completed_record_hashes()`（只认 `status=completed`）并单测。
31. **auto × delete_sync 双向错**：小数据走 sync_transfer 被"注入守卫"跳删（router 自取源与 webhook 推入不可区分）、大数据硬失败。修法：router 对 auto+delete_sync 入口显式拒绝（与 async 同口径）。
32. **异步通道 DB 推送从不 upsert**：不传 key_field/mode，任务默认 async 即踩——有唯一约束则 IntegrityError 永久失败、无则静默重复。修法：与同步通道同口径；`tests/test_security_fixes` 把错误行为断言成期望的用例已改正。
33. **非 JSON 安全值进哈希/JSON 列**：Oracle/MySQL/PG 源（datetime/Decimal）的 full 同步在幂等检查或写 buffer 即 TypeError。修法：`compute_hash` 加 `default=str` + 引擎级 `json_serializer`（`app/database.py`）。
34. **HAP `_stable_scan` 排序参数写错**（`isAsc`，其余全部为 `sortType`）→ 确定性分页失效，删差键集合有缺口风险。~~修法：改为 sortType~~ **【真机回归推翻（2026-10-01）】**：参数矩阵实测证明 `isAsc` 才是真实的方向参数（true=升序/false=降序），`sortType` 的值被忽略——原代码 `isAsc:true` 本就正确，属**审查误报**；已还原，并留下参数矩阵脚本 `scripts/hap_sorttype_check.py` 为证。教训入档：与 P4 已验证行为不一致的"修复"，先查当年验证依据再动手（AGENTS 陷阱 18 的完整形态）。
35. **`delete_rows` 单条 IN 不切块**：Oracle ORA-01795（1000）/SQLite 变量上限 → 大表首次对账删差永久失败。修法：SQLite 500/Oracle 1000/MySQL 1000 分块（单事务或 autocommit 语义不变；PG 走 `= ANY($1)` 数组无需处理）。
36. **SQLite upsert 无约束前提**：key_field 无 UNIQUE/PK 时 `ON CONFLICT` 抛驱动原文（旧 INSERT OR REPLACE 是静默重复，同样错）。修法：翻译为 `upsert_key_not_unique` 中文可行动错误。
37. **探针 `SideInput.auth_type` 无白名单**：未知/错拼类型让脱敏与加密双双 fail-open，明文凭据落 `probe_sessions.source_snapshot` 并回显。修法：SideInput 白名单校验 + `sensitive_auth_fields` 对未知/空类型 fail-closed（`desensitize_auth_config`/`encrypt_auth_config` 的 `not auth_type` 早退一并拆除）。
38. **绝对 endpoint 绕过 base_url**：v1 `prepare_request` 原样放行绝对 URL（httpx 语义覆盖 base_url）→ 已存连接凭据外送任意主机、SSRF 白名单失效。修法：v1/v2 统一 `resolve_target_url`；探针 `SideInput.endpoint` 只允许相对路径。
39. **BI「其他」归并项与真实维度值撞 `dim_hash`**：唯一索引冲突 → 整窗聚合失败、水位永不前进、指标永久停更。修法：存储层哨兵 `__others__`（`OTHERS_KEY`），读取/即席出口经 `display_dims()` 还原（顺带修好 raw/table 的 truncated 提示）。

**已确认 P2**

40. BI 列表端点 fail-open（`scope_metric_list` 的 `if placed and ...`，与 §3.1-17 同根因残留）→ fail-closed。
41. 保存卡路径绕过列隐藏（measures 只按 key、computed 不查）→ `scope_defn` 三口径（key+field+computed 引用），`scope_buckets` 值层改按剥离后度量 key 白名单（原拿 key 比对隐藏字段名，改名度量从 raw/table 泄漏），`query_metric` 先 scope_defn 后 scope_buckets。
42. 数值维度筛选整卡空白（桶里 5.0 vs 前端 "5"）→ `_dim_eq` 归一比较（字符串优先、float 回退）。
43. detail 卡 `/metrics/{key}` 无上限（一次 10 万行）→ 截到 `DETAIL_PAGE_MAX`。
44. 改 `schedule_cron` 不重算 `next_run_at` → 更新时即重算（空 cron 置 None）。
45. metrics 延迟/趋势、monitoring 错误聚合全窗口读进内存 → 加采样上限（10 万/5 万行，超限近似并注释）。
46. `redact_text` 漏 JSON 引号形态与 Bearer 值、`app-key` 连字符变体 → 两段式正则（bearer 先行）。
47. WebSocket 适配器丢弃 query 型凭据（取完即丢，握手无凭据）→ 拼回 URL。
48. 前端：看板设置读取失败被吞成空设置 → 空态"添加卡片"以 `{cells:一张卡}` 全量覆盖看板；改为失败保留最后一次已知设置 + `loadError` 门禁（空态不显示、save 拒绝并提示）。连接保存失败的 422 detail 整段 `JSON.stringify` 上屏（含 auth_config 明文）→ 只提取 loc/msg。

**待用户拍板（未修复）**：PostgreSQL aware-datetime 编码（需真机复现）、probe approve 并发、mapping_id 存在性、PUT null→500、probes 清单分页、count_distinct 不可哈希、定义更新不清桶、gRPC 模块名、非分页源无上限、retry_count 死配置、sweep_idle_pools 未接线、前端竞态族与杂项、安全 P3（/docs 暴露、非 ASCII 令牌 500 等）——明细见审查报告。

### 3.3 第四轮：剩余清单全量修复（2026-10-01，用户拍板"全部修复"）

**平台/模型层**

49. **PostgreSQL aware-datetime（上轮待复核项）系统性修复**：新增 `UtcTimestamp` TypeDecorator（aware → naive UTC 绑定；读取不加工，SQLite/历史数据语义零变化），sync/bi 两模型全部 35 个 TIMESTAMP 列换用；探针域保留 `ProbeTimestamp`（+08 语义）。建议后续对真机 PG 补一次部署级复现。**【已验证 2026-10-01】**本机 PG 15.5 实测通过：失败模式实锤（原生 aware 绑定仍被拒）、35 列建表/插入/naive UTC 回读、JSON 列 datetime/Decimal、真库跑 API 测试 65+52 passed、全新库迁移首装 16 表——详见 VALIDATION.md 五轮记录与 `scripts/pg_live_check.py`。
50. `mapping_id` 存在性校验（创建/更新）——SQLite 未开外键，原可建出"永远静默不执行"的僵尸任务。
51. PUT 显式 null 命中非空列 → 422（`app/api/_guards.py::reject_null_non_nullable`，tasks/connections/mappings 三端点统一；可空列的 null 保留清空语义）。
52. 探针 approve 并发守卫（条件 UPDATE 占位 + 复检，双击/双管理员不再重复建桥）+ 悬挂引用守卫（引用已删连接 → 422）。
53. `GET /probes` 分页（limit/offset，默认 200）。
54. 探针内联明文凭据缓存：24h TTL 惰性回收 + 会话终态（approve/reject）即清。

**BI 引擎**

55. `count_distinct` 对 list/dict（HAP 多选/附件）稳定字符串化去重，不再 TypeError 打断整窗聚合。
56. 指标定义更新检测口径变化（源/粒度/维度/度量/过滤等）→ 自动清空该指标全部桶并删水位线（改 granularity 后时间轴错位、回填跨桶翻倍的根因）。
57. 水位线"只允许向前"落到 SQL 条件更新（调度轮与手动聚合并发不再回退；naive 比较兼容 ORM 会话内评估器）。
58. `last_used_at` 真正落库（get_viewer 内提交；admin 身份 no-op）——授权档案"最近使用"不再恒空。
59. 表达式引擎按引号切分字面量：`concat([产线], "[A线]")` 的字面量不再被引用替换改写/误报（extract_refs 与重写、未解析检查三处一致）。
60. 度量 key 保留形态 `__ratioN__(num|den)` 保存期拒绝（防下推别名覆盖串位）。

**适配器/连接器/调度**

61. 非分页源（`pagination.mode=none` 默认值）也受行/字节双上限约束。
62. gRPC proto 按内容哈希唯一模块名加载（不再命中 sys.modules 旧契约、sys.path 不再膨胀）。
63. REST GET 读数据按连接 `retry_count` 幂等重试（指数退避，仅瞬时错误；4xx 不重试）——retry_count 从死配置变为生效。
64. REST/Dubbo 连接测试 4xx 判失败（401/403 给"鉴权失败"提示，404 给"探测端点不存在"）。
65. `sweep_idle_pools` 接入调度循环（连接改配泄漏旧池/旧密码会话常驻的问题）。
66. 探针 DB 侧纳入 SSRF 防线（`_db_connector` 对 db_config.host 套 validate_target，enforce 开启时生效）。

**安全**

67. 生产环境关闭 `/docs` `/redoc` `/openapi.json`（dev 保留）。
68. 非 ASCII 令牌（HTTP 头 latin-1 放行字节）→ 401，不再 500（auth 与 webhook secret 两处）。
69. `desensitize_record` 提示词补 credential/auth/bearer/jwt/session；URL 清洗扩展到 `*url/*uri/*endpoint/*link/*href` 尾缀键（HAP query 鉴权不再直穿）；`url.port` 非法端口不再抛 ValueError（500 根因）。

**前端**

70. 竞态守卫：字段字典抽屉（请求序号，防 A 表字段存进 B 表字典）、监控看板（fetchSeq/drillSeq）、日志列表（store 级序号）。
71. 压测/发送类长请求纳入 axios 超时豁免（UI 允许 60s > 原 30s 截断）；数据比对 JSON 解析错误分别提示。
72. 分析页"另存为"画布满时明确提示，不再写 `cells[key]=null` 假成功。
73. 驳回弹窗成功后关闭并清空原因（不再打到 /probes/undefined）。
74. 复制投放链接：clipboard 不可用回退 execCommand，再失败弹窗展示手动复制（http 部署不再假成功）。
75. mappings 删除加 Popconfirm（与其它破坏性操作一致，409 后刷新保持一致）。
76. BiGate 错误态有限重试（5s×6，无人值守大屏自愈）；三处轮询加 visibilitychange（隐藏暂停/恢复即刷）。

**回归**：`tests/test_review_round3.py` 18 项 + 既有用例对齐；全套 **1136 passed / 13 skipped / 0 failed**；前端 `npm run build` 重建 `web/`。

### 3.4 五轮复审：修复 diff 再审核（2026-10-01，4 路并行 + 全量修复）

对 98c3f21..HEAD 全部修复 diff 做闭环/回归/遗漏三问复审。**结论：主体闭环扎实；发现 3 P1 + 5 P2 + 若干 P3，其中 3 项是此前修复自身引入的回归——全部当轮修复**：

**P1（含 2 处"修复引入的回归"）**

77. **gRPC 唯一模块名加载首编译即失败**（§3.3-62 的回归）：pb2_grpc 顶部 `import service_pb2` 不可导入 → ModuleNotFoundError，gRPC 全路径不可用。且复审实测发现更深一层：protoc 产物按**文件名**注册全局描述池，固定名 service.proto 让同进程第二个不同 proto 直接撞池。终态：proto 文件名带内容哈希（`svc_<hash>.proto`），产物名天然唯一、描述池/模块名双唯一；同 package+同名消息是 protobuf 进程级硬约束，包装为可行动错误提示。回归测试锁定。
78. **`decrypt_auth_config` 未跟上加密侧 fail-closed**（§3.2-37 的回归）：auth_type 为空时加密全量、解密早退——密文被当真实凭据注入，该连接所有 API 调用认证必挂。修法：解密侧对称 fail-closed，round-trip 测试锁定。
79. **即席 ratio 分量绕过列隐藏**（§3.2-41 的同族缺口）：scope 侧修了 key+field+ratio 分量，adhoc 侧只查 key/field。修法：复用同一判定。

**P2**

80. probe DB 侧 SSRF（§3.3-66）用 `tcp://` 硬造 URL 被 scheme 门无条件拒——enforce 开启后 DB 探针 100% 挂。修法：新增 `validate_host`（同拒绝段/白名单语义，无 scheme 门）。
81. probe_l1 的 TargetNotAllowed 处理器 rollback 后访问过期 ORM 属性 → MissingGreenlet 500（§3.1-16 的接线缺陷）。修法：先 transition/commit 再 rollback。
82. websocket `test_connection` 漏改（§3.2-47 的同文件兄弟点）：query 型凭据在连接测试仍丢失。
83. probes 清单分页与后置过滤次序颠倒（§3.3-53 的回归）：level/protocol 是 Python 侧派生属性，先切页再过滤会静默漏项。修法：全部过滤后切片。
84. `app/probe/timestamps.py` 编辑事故整块重复（§3.2-49 遗留）→ 去重重写。
85. rest GET 重试注释称限流可重试但 429 实际不重试 → 429 归入可重试。
86. desensitize URL 清洗收窄回归（§3.2-69 引入）：ws:// 与相对路径的 query 不再掩 → 放宽为按键尾缀清洗、失败兜底原样。
87. `normalize_computed` 保存期归一仍是全局 re.sub（§3.2-59 的同族三处之一，损失不可逆）→ 字面量段保护。
88. 前端：loadError 页内恢复死路（刷新按钮不重试设置读取）→ hook 暴露 reload 并接线；FieldDictDrawer catch/finally 未守 seq（半闭环）→ 补齐；驳回失败仍关弹窗 → act 返回成功布尔；复制回退未查 execCommand 返回值；dashboard finally 未守 seq；bi/index+screen 轮询的 visibilitychange"注释宣称未兑现" → 兑现。

**P3/文档**

89. `app/security/desensitize.py` 孤字 `auth` 提示词误伤 author/authors（四轮引入，自查发现）→ 改 `authkey`。
90. 文档修正：§3.1-12 的 `hap_delete_write_mode` 错误码不存在（实际为 push 强制去重）→ 已更正；VALIDATION.md 大面积重复粘贴 → 重写为整合版；§6 计数修正。

**复审确认闭环的代表项**：UtcTimestamp 全列 naive 一致性（全部比较点逐一核实）、approve 条件 UPDATE 的 PG 行锁语义、删差守卫双键与纵深防御、async upsert 双通道对齐、scope 三口径、「其他」哨兵全部读取出口还原、水位条件更新（evaluate 同步不标 dirty 实证）——详见复审报告。

---
---
---

## 4. 有意**不移植**（目标方决策，已在文档侧标注）

| 项 | 原因 |
|----|------|
| HAP 元数据 V3 双实现（`list_worksheets_v3/v2`、`get_worksheet_controls_v3/v2`、`get_worksheet_detail_v3`） | 目标线收敛为 V2 单链路（`/v1/open/app/get` + `getWorksheetInfo`），合并保留该实现；BI 的 HAP 取数与 P4 增量均在 V2 上实测（`filters` + `sortId`）。如需空工作表场景的 V3 回退，需单独提案 |
| `DELETE /api/v1/connections/{id}/fields` | 目标线在字段策略重构中移除；两端前端与测试均无调用方（已核） |
| `frontend-source` 下的 Dockerfile / nginx.conf / .gitignore 等 | 属 GPOST 前端容器化脚手架，发行包不带；GNPost 仓库内仍可查 |

## 5. 有意**保留但默认不启用**

- **明细保留清理**：`bi_scheduler.purge_expired_buffers()` 保留为可调用能力，但**不接入自动循环**——合并后的 `DataBuffer` 是增量轮次的工作集（`round_id`/`batch_no`/`attempt_token`/`lease_until`/`next_retry_at`），按年龄删除会破坏在途轮次与失败重试；发行包对用户的承诺是"BI 不会删除同步缓冲"（见 `bi-merge-guide.md`）。需要时由运维显式调用或另接维护任务。
- **无锁降级**：目标线在 Redis 不可用时**跳过本轮**（不降级为无锁聚合）；本合并沿用（多 worker 下更安全）。GPOST 的降级实现留在 `E:\_merge_ws` 历史里。

---

## 6. 验证结果（全部在本机实测）

| 项 | 结果 |
|----|------|
| 路由表并集 | 合并后覆盖两侧并集的 **99/100**；缺的 1 条为 §4 有意移除项（脚本 `_merge_ws/_tools/dump_routes.py` 可复跑） |
| 后端测试（工作区） | 见 §3.4 末尾各轮计数（最新 1136+ passed / 0 failed） |
| 后端测试（**发行包目录内**，不设任何环境变量） | 同上（各轮均为 0 failed） |
| 前端类型检查 | `npm run build`（`tsc -b` 项目级真 gate）0 错误——注意裸 `tsc --noEmit` 对 solution 型根配置是空转，必须用 build |
| 前端构建 | `vite build` 成功，产物落 `web/`（含 `analysis`/`access` 分块） |
| 升级迁移 | 对既有库（无 BI 表、无 schema_migrations）执行 `python -m app.migrations`：5 张 BI 表建齐（含 P6 新表 `bi_access_profiles`）、版本标记写入、存量表与数据（7 条连接）完好 |
| 运行时冒烟 | `uvicorn server:app` 起服：`/health` 200、SPA `/` `/bi/screen` `/bi/analysis` 200、`/api/v1/connections` 200（7 条）、`/bi/metrics`、`/bi/screen-settings`、`/bi/access/profiles`、`/metrics/tasks` 均 200、无令牌 401 |
| P6 只读令牌链路 | 管理令牌建档 → 拿到 `bit_…` 令牌 → 带 `X-BI-Token` 免管理令牌读 200；受限身份打写端点 403；无令牌 401 |
| **浏览器全功能实测**（2026-09-30，Playwright/IAB 真操作） | 见下方"浏览器实测清单"，全程**零控制台错误、零失败请求**；抓到并修复 §3 的 7/8 两处问题 |

### 浏览器实测清单（在发行包内起服，源/目标 SQLite 真实数据）

| 环节 | 操作与证据 |
|------|-----------|
| 连接 | UI 新建 SQLite 源/目标连接 → "测试"返回"连接成功，延迟 0ms"；字段抽屉列出 7 字段（名称/类型/可空/主键），取消 `category` 可写并保存 → 接口回读 `writable=false`、7 项 `saved` |
| 表单注册表 | UI "从源端拉取" → "拉取完成：新增 1"（`products`），未选连接时正确提示先选连接 |
| 字段映射 | UI 建映射（7 字段对 + 目标主键 `id`）→ "映射已创建"；编辑开启"删除同步"→ `delete_sync=true` |
| 同步任务（真实搬运） | UI "运行"：目标库 5 行落地（数值/日期类型正确）；改源（删 1、改 1、增 1）后再跑 → 目标**删除 1 行、更新 1 行、新增 1 行**，执行日志 `source_read/push/delete` 全 success（验证了 §3 第 1 条的 `delete_sync` 兼容修复） |
| 执行日志 | 列表分页/动作/耗时正常；失败行"详情"下钻显示失败阶段、错误代码、异常类型、**代码位置（帧）**与错误明细 v1.0 |
| BI 指标定义 | UI 建 form 源指标（维度 `category` + 度量 `sum(qty)`）→ 保存 → 列表启用；`POST /bi/aggregate/run` 后图表数值 **五金件 91 / 包装材 999 / 注塑件 120 / 电子料 45**，与源数据逐项一致 |
| BI 大屏 | 布局编辑加入卡片并自动保存 → 图表渲染（柱状，中文轴标签） |
| 投放模式 | 管理侧"投放模式"打开 `/bi/screen?board=default`：无管理导航、卡片与图表正常、独立投放 |
| 分析工作台 | 选连接/表单 → 点字段出图，标签显示 **"聚合下推 · 1ms · 分组 4"**（P3 下推在 UI 侧如实标注） |
| 访问授权（P6） | UI 建只读档案（行过滤 `category:电子料`）→ 令牌仅展示一次 → 投放链接在**清空管理令牌**的标签页打开：令牌自动入 localStorage、地址栏参数被抹掉、屏幕页正常进入；受限身份取数 **只返回 `电子料`（值 45）**；隐藏列=该卡唯一度量时**整卡不返回**（"宁可少给"语义） |
| 监控看板 | KPI 与操作对得上：同步总量 4、成功率 75%、P95 26ms、任务健康度 3 成功/1 失败/2 缓冲批次、错误 Top N 列出那次 `ValueError` |
| 其余页面 | 监控看板、连接管理、表单注册表、字段映射、同步任务、执行日志、收发比对、联调探针、待确认清单均正常渲染，无控制台错误与失败请求 |

环境备注（与产品无关，但现场会碰到）：本机 6379 上是旧版 Redis（不支持 HELLO 握手），redis-py 8.x 连接即抛 `ResponseError`；同步任务取锁失败会以 500 暴露（"锁不可用即拒绝执行"是既有语义）。发行包自带 Redis 8.10.1（端口 16379）不受影响——实测环境改用包内 Redis 后一切正常。

测试口径变化说明：原 GPOST 基线 1037 项，合并后 1012 项。差异来自**按合并后实现重写的模块**（`test_field_dictionary` 由 field_dict 纯函数级改为 field_policy 行为级；`test_hap_list_worksheets` 由 V3/V2 双实现改为 V2 单链路）与**随实现下线而退役的用例**（同步引擎内联水位推进、`get_rows_with_key` 去重路径等）。发行包自带测试在其仓库里但未随包发布，因此目标线自身特性的用例（增量轮次、HAP 批量 upsert、水位管理）在本仓库中**暂无覆盖**，属已知缺口。

---

## 7. 发行包内新增/变化的内容

- `app/`：合并后端（`app/services/` 由 34 个模块增至 44 个，含 P1–P6 的 `bi_access.py`/`bi_adhoc.py`/`connectors/grouped_pushdown.py`）
- `frontend-source/`：合并前端源码（含 `pages/bi/analysis.tsx`、`pages/bi/access.tsx`）
- `web/`：由合并后前端重新构建
- `tests/`、`pytest.ini`：**新增**（发行包此前不带测试），已自足化（临时 SQLite + 测试密钥默认值，不依赖 `.env`）
- `openspec/`、`docs/`、`scripts/`、`configs/`、`docker-compose.yml`、`Dockerfile`、`AGENTS.md`、`CLAUDE.md`：**新增**（开发资产，随合并带入；它们是 `configs/alerts.yml`、`configs/grafana-dashboard.json`、`docker-compose.yml` 相关用例的依赖）
- `MANIFEST.sha256`：**重建**。覆盖范围与原包一致（runtime/oracle/redis/app/frontend-source/web/skills），根文件由原 5 个扩到 8 个（补 `manage.py`、`migrate.cmd`、`requirements.lock`——它们本就随包发布，纳入校验更完整）；条目 4832 条
- `README.md`、`bi-merge-guide.md`：补充本次合并说明（见下）
- 未改动：`runtime/`、`oracle/`、`redis/`、`prerequisites/`、`launcher` 相关脚本、`requirements*.txt`

---

## 8. 上线注意（与合并前的行为差异）

1. **webhook**：未配 `secret` 的任务现在返回 409（`webhook_secret_required`）。迁移前请给所有 webhook 任务补 `webhook_config.secret`（或显式设 `APIBRIDGE_WEBHOOK_REQUIRE_SECRET=false`，仅建议在已有网络层防护时）。
2. **BI 门禁**：BI 端点带启用门禁（`APIBRIDGE_BI_ENABLED=false` 时全部 404，`/bi/status` 例外）。启用方式见 `bi-merge-guide.md`。
3. **CORS**：不再使用 `allow_origins=["*"]`；默认放开本机前端端口，跨机访问请设 `APIBRIDGE_CORS_ORIGINS`（逗号分隔）。发行包同源提供前端，正常使用不受影响。
4. **HAP 目标删除对账**：`delete_sync`（既有）与 `hap_delete_sync`（新增）两键同等生效；对账仍要求全量 sync 且源集合完整（视图/过滤会拒绝）。
5. **指标**：`apibridge_queue_depth{queue_name}` 与 `apibridge_mq_queue_depth{queue_name}` 同值双写；MQ 不可达时两者都不导出。
6. **调度器**：空 `schedule_cron` 的定时任务静默跳过（不再每 5 分钟写库+告警）；填了但无效的 cron 仍会推迟并告警。
7. **表单读取上限**：BI 表单/窗口超过 `min(MAX_FORM_ROWS, APIBRIDGE_BI_MAX_ROWS)` 时**拒绝**返回（不再截断）；大表 DB 源走聚合下推不受此限。

---

## 9. 如何继续开发

- 合并后的历史与可复核过程在 `E:\_merge_ws`（分支顺序：`target-side` → `merged`），如需正式并入某个 git 仓库，可直接以该仓库为远端推送或按需 rebase。
- 在本目录（发行包）跑测试：`runtime\python.exe -m pytest tests/ -q`（无需任何环境变量；当前基线 1078 passed / 13 skipped）。
- 前端改动：在 `frontend-source/` 内 `npm ci && npx tsc --noEmit && npx vite build`，产物替换 `web/`。
- 结构变更前先读 `openspec/specs/<域>/spec.md`；行为变更走 `openspec/changes/`。

## 10. 浏览器实测沙箱（可删）

本次实测在包内起了一个临时环境，供你自行点开复核：

- 服务：`http://127.0.0.1:8124`（管理令牌 `wt-admin-token`）；后台还起了包内 Redis（端口 16379）。
- 数据目录 `data_wt/`：平台库 `app.db` + 源库 `source.db` + 目标库 `target.db` + 连接/映射/任务/只读令牌（令牌 `bit_70a80554df87096c588409b317f7f2e2`，行过滤 `category=电子料`）。
- **生产管理看板**（基于 `E:\GPOST\data\production_test.db`，71MB 生产测试库）：
  看板 `http://127.0.0.1:8124/bi/screen?board=production`，17 张卡（6 KPI + 10 图 + 明细表，72×36 三层布局、深色主题）；
  生成脚本 `scripts/seed_production_board.py`（幂等可重跑，`--check` 只做数值对照）；
  实测数值与源库直查逐项一致：计划 31500 / 实际 30848 / 达成率 97.93% / 合格率 97.62% / 利用率 88.27% / 出勤率 96.59% /
  一车间 20088、二车间 10760 / 占比 34.3·30.8·34.9% / 百万行表 5 线聚合（走源库 GROUP BY 下推）。
- 关闭：结束占用 8124/16379 的进程（`stop.cmd` 只管包自身默认端口，不影响这里），然后删除 `data_wt/` 即可。`MANIFEST.sha256` 不包含 `data_wt/`，不影响发行校验。
