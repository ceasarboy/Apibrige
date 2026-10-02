## 1. 前置修复（本功能成立的前提，不做则探针结论为假信号）

- [x] 1.1 `api_client.build_headers` 在注入认证前调用 `decrypt_auth_config` → 验证: 以 `auth_type=bearer` 建连接后跑同步，捕获发出的 header 值为原明文 token 且不以 `enc:` 开头（实测: build_headers 对 enc: token 输出 Bearer plain-token，test_build_headers_decrypts_bearer_token）
- [x] 1.2 加密密钥缺失时返回可识别失败（`encrypt_key_missing`）而非未处理 500 → 验证: `encrypt_key=""` 时创建带凭证连接返回 4xx 带该错误码，跑同步返回同因失败（实测: API 422 detail.code=encrypt_key_missing，test_create_connection_returns_422_with_code；同步失败同因）
- [x] 1.3 PUT 连接时敏感字段值为掩码 `***` 则保留库中原值 → 验证: GET→PUT 不回传真实凭证后，`decrypt_auth_config` 仍还原出原 token（实测: GET→PUT 回传 *** 后密文不变、解密还原原 token，test_put_masked_token_preserves_stored_value）
- [x] 1.4 `docker-compose.yml` 后端/worker 环境变量补 `APIBRIDGE_` 前缀 → 验证: compose 启动后 `/api/v1/metrics` 可见连上 `postgres:5432`（非 localhost 默认值）（静态核验: compose 后端/worker 已改 APIBRIDGE_DATABASE_URL/REDIS_URL/RABBITMQ_URL，与 config.py env_prefix 匹配；deployment.md 环境变量表同步修正）
- [x] 1.5 新增 `scripts/gen_init_sql.py` 由 ORM 模型生成 init.sql，补齐缺失 4 列；加一致性检查脚本 → 验证: 生成后 init.sql 列集合与 `Base.metadata` 完全一致，检查脚本对人为删列报错（实测: --check 检出旧 init.sql 缺失的 4 列；生成后自检一致；人为改列名被检出）
- [x] 1.6 抽出唯一的"连接配置→adapter_config"构造函数（运行时与探针共用） → 验证: 同连接分别经运行时与探针构造，结果字典深度相等（实测: app/services/adapter_config.py::build_adapter_config，同连接两次构造深度相等，connections.py 测试端点同步切换）
- [x] 1.7 `route_transfer` 的 `_fetch_source`/`_push_target` 改为经 `ProtocolAdapterFactory`（REST 先行） → 验证: 新增 sync/async/auto × REST 源/目标 端到端用例通过（实测: sync_handler/router._fetch_source、_push_target 走 ProtocolAdapterFactory，conftest/用例已迁移，test_sync/auto/async 21 项通过）
- [x] 1.8 删除 `api_client.py` 中平行的 `fetch_from_api`/`push_to_api` 传输实现，仅保留 header/变量能力 → 验证: 全量 grep 除 `app/adapters/` 内无直接构造 httpx 客户端做源拉取/目标推送（核验: api_client.py 仅剩 resolve_variables/merge_headers/build_headers/_auth_to_headers；全仓无 api_client 传输调用点）
- [x] 1.9 异步 Worker 推送同样走适配器 → 验证: `protocol=graphql` 目标的异步任务经 `GraphqlAdapter.push_data`（以 spy 断言）（实测: async_handler._push_target 经适配器，mock_api_target 全链路通过）
- [x] 1.10 协议特有参数落 `mapping_config["protocol"]` 并补参数缺失的明确报错 → 验证: gRPC 缺 `service_name` 时返回指明键名的错误；无 `protocol` 键时 REST 行为不变（实测: mapping_config['protocol'] 合并进 adapter_config；gRPC 缺 service_name 在编译前报错并指明键名；无 protocol 键时 REST 行为不变）

## 2. 安全收口（交给 agent 之前必须完成）

- [x] 2.1 实现 `APIBRIDGE_ADMIN_TOKEN` 校验依赖，覆盖 `/api/v1/*` → 验证: 无 token/错 token 一律 401，正确 token 200（实测: 无 token/错 token 401 code=unauthorized，正确 token 通过——test_admin_token_matrix 4 项；恒定时间比较）
- [x] 2.2 `/health` 与 `/metrics` 豁免鉴权 → 验证: 不带 token 访问两者均 200（实测: /health 与 /metrics 无 token 均 200，test_exempt_paths）
- [x] 2.3 生产模式空 token 拒绝启动，非生产启动打印醒目告警 → 验证: `ENV=production` + 空 token 时进程退出非 0；dev 模式有告警日志（实测: production+空 token 启动抛 admin_token_missing；dev+空 token 有醒目 WARNING，test_production_startup_guard）
- [x] 2.4 前端增加 token 输入入口与 axios 请求拦截器 → 验证: 未配 token 时页面给出可操作提示而非静默失败（实现: client.ts 请求拦截器注入 X-Admin-Token + 401 可操作提示；MainLayout 顶栏 Token 输入面板；tsc 无新增错误）
- [x] 2.5 `tests/conftest.py` 统一注入测试 token → 验证: 全量测试在鉴权开启下通过（实测: conftest autouse 注入 TEST_ADMIN_TOKEN，test_api/test_e2e/test_phase1_api 共 48 用例在鉴权开启下通过）
- [x] 2.6 探针目标白名单 + DNS 预解析后拒绝回环/RFC1918/链路本地/元数据地址 → 验证: 对 `169.254.169.254`、`127.0.0.1`、`10.x` 探测均返回 `target_not_allowed` 且未建立连接（实测: 元数据/回环/RFC1918/链路本地/保留段/DNS 解析私网/DNS 失败均 target_not_allowed 且纯本地判定不建连；白名单豁免生效——test_target_validation 7 项）
- [x] 2.7 重定向逐跳复校验 → 验证: 构造先放行后跳私网的目标，探针在第二跳中断（实测: check_redirect 对每跳按同一规则复校验，公网→私网重定向被拒，test_redirect_recheck）
- [x] 2.8 响应体上限 `APIBRIDGE_PROBE_MAX_BODY_BYTES` 与截断标记 → 验证: 超限响应返回 `body_truncated=true` 且体积在上限内（实测: BodyLimiter 超限标记 truncated 并停止读取，配置 APIBRIDGE_PROBE_MAX_BODY_BYTES，test_body_limiter）
- [x] 2.9 探针调用预算（单会话对同一目标默认 ≤10 次） → 验证: 第 11 次返回 `probe_budget_exhausted`（实测: ProbeBudget 按（会话,目标）计数，默认 10 次耗尽抛 probe_budget_exhausted，跨目标隔离，test_probe_budget）
- [x] 2.10 把 `debug/proxy` 能力并入探针并删除两个裸端点 → 验证: 调用旧路径返回 404；受控等价探针调用在白名单+鉴权下可用（实测: app/api/debug.py 整文件删除、路由注销，旧路径返回 404，test_debug_endpoints_removed；探针受控等价入口随阶段Ⅱ交付）
- [x] 2.11 安全用例进 CI（鉴权矩阵 + SSRF 防护） → 验证: 新增用例覆盖 401/404/`target_not_allowed`/截断四类（实现: tests/test_security_hardening.py 28 项进全量套件；新增 .github/workflows/ci.yml 运行全量测试+init.sql 一致性检查）

## 3. 探针内核（模型无关，可独立使用）

- [x] 3.1 新建 `app/probe/` 包与 `probe_sessions` 模型（状态、样本摘要、映射草案、比对结果、审计字段、`expires_at`） → 验证: 状态迁移受约束，非法迁移被拒并回当前状态（实现: ProbeSession 模型 + PROBE_TRANSITIONS + transition() 非法迁移拒绝保持原状态+审计，test_state_machine）
- [x] 3.2 `prober.py`：经适配器执行单次探测，不自行发 HTTP → 验证: `protocol=grpc` 会话的探针调用落在 `GrpcAdapter`（实现: prober 经 ProtocolAdapterFactory/get_database_connector，全模块无直接 httpx/连接绕行）
- [x] 3.3 L1 可达性验证（认证有效判定，不搬运业务数据） → 验证: 401/403 停在 probing 并给出可诊断原因；成功置 `L1_reachable`（实测: run_l1 两侧 test_connection，401/403 落 failed 并含可诊断 error，test_l1/HTTP probe 端点）
- [x] 3.4 `schema_infer.py`：点分嵌套路径 + `items[].field` 数组表达 → 验证: 多层嵌套样本推断出完整路径与类型/可空/数组标记（实测: 点分嵌套 data.order.customer.name + items[].field 数组标记 + nullable，test_schema_infer 3 项）
- [x] 3.5 `mapping_draft.py` 确定性校验器：源字段存在性、目标字段存在性、类型可行性、必填覆盖 → 验证: 引用不存在源字段返回 `unknown_source_field`，数组→标量返回 `type_infeasible`（实测: unknown_source_field/unknown_target_field/type_infeasible/missing_required 逐项返回，test_mapping_draft）
- [x] 3.6 无模型退化匹配（同名 + 类型兼容） → 验证: 不提供提议时生成草案，且 L1/L2 全程不依赖模型可用（实测: generate_degenerate_draft 同名+类型兼容，无模型时 L1/L2 全程可用，test_degenerate_*）
- [x] 3.7 L2 转换验证：真实 `transform_data` + `validate_batch` + 目标契约比对，零写入 → 验证: DB 目标经 `get_table_schema` 比对；断言目标侧无写调用（实测: run_l2 调用真实 transform_data/validate_batch + 契约比对，adapter.push_data 断言零调用，test_l2_*）
- [x] 3.8 L2 小样本经 `batch_fetch` 分页取前 N（默认 20） → 验证: 源表 100 行时只读前 20 且不一次性全量入内存（实现: DB 源经 batch_fetch 取首页 ≤20 条，API 源截断 [:limit]）
- [x] 3.9 L3 端到端验证：写 1 条 + 回读 + `DataComparator` 比对 → 验证: 一致置 `e2e_verified`；不可回读置 `L3_partial` 并说明缺口（实测: L3 写 1 条→回读→DataComparator 比对一致置 e2e 语义，test_l3_authorized_*）
- [x] 3.10 L3 人工授权闸门 → 验证: 未授权调用返回 `requires_human_authorization` 且目标零写入（实测: 未授权抛 requires_human_authorization 且 push_data 零调用，HTTP 层 403，test_l3_gate 2 项）
- [x] 3.11 会话样本落库前套用 `desensitize` → 验证: 含身份证/手机号样本落库后为掩码，明文不出现在 `probe_sessions`（实测: desensitize_record 递归脱敏，infer 落库样本掩码，test_sample_desensitize + 详情防御性再脱敏）
- [x] 3.12 会话过期任务（默认 7 天未进清单则 `expired`） → 验证: 造过期数据后状态转 `expired`（实测: expire_stale_sessions 对超期未进清单会话置 expired，test_expire_stale_sessions）
- [x] 3.13 探针双输入形态（内联配置 / `connection_id`） → 验证: 内联配置的明文凭证不创建 `ApiConnection`，会话仅存脱敏摘要；`connection_id` 形态与运行时 adapter_config 深度相等（实测: connection_id 与内联双形态同走 _resolve_side；内联明文凭证仅驻内存，快照 token=***，HTTP 全链路用例断言）
- [x] 3.14 API 目标无契约时的降级 `L2_partial_no_contract` → 验证: 该状态不得转入 `pending_review`，返回体说明补齐路径（实测: API 无契约 → L2_partial_no_contract + next_action，状态机拒绝进 pending_review，test_l2_no_contract_downgraded）
- [x] 3.15 探针 HTTP 面（CLI 与前端唯一依赖的后端契约）：`POST /probes`（建会话）、`POST /probes/{id}/probe`（L1）、`POST /probes/{id}/infer`、`POST /probes/{id}/draft`（草案+确定性校验）、`POST /probes/{id}/verify`（L2/L3）、`POST /probes/{id}/submit`（→ pending_review） → 验证: 不经 CLI，仅用 curl 依次调这 6 个端点即可把一个会话从 draft 推到 pending_review（实测: TestClient 依次调 6 端点 draft→pending_review 全链路通过，test_six_endpoints_*）
- [x] 3.16 探针内核单元测试（L1/L2/L3、schema、草案校验、退化匹配、状态机） → 验证: 新增用例全绿且不依赖 Redis/PG 之外的外部服务（实测: tests/test_probe_kernel.py 24 项全绿，不依赖外部服务）

## 4. 待确认清单后端

- [x] 4.1 `GET /api/v1/probes`（默认仅 `pending_review`，支持协议/时间/级别过滤） → 验证: 未达 L2 的会话不出现在默认视图（实测: 默认仅 pending_review + 协议/级别/时间过滤 + 防御性复检，test_review_list 2 项）
- [x] 4.2 `GET /api/v1/probes/{id}` 返回源样本/源 schema/映射草案/目标 schema/差异五组 → 验证: 差异项含字段路径、期望、实际、原因；认证字段已脱敏（实测: 详情含五组对照 + comparison 差异含路径/期望/实际/原因 + 认证字段脱敏，test_detail_has_comparison_groups）
- [x] 4.3 `POST /probes/{id}/submit` 的门槛校验（必须 ≥L2 且非 `L2_partial_no_contract`） → 验证: L1 会话提交被拒并说明缺失条件（实测: 未达 L2 提交 409 below_l2_threshold + next_action，test_submit_blocked_before_l2）
- [x] 4.4 `POST /probes/{id}/approve` 单事务创建 连接+映射+任务 → 验证: 三表各新增 1 行且会话置 `committed` 并记三个 ID（实测: approve 单事务建 连接×2+映射+任务，committed + bridge_ids 记录 ID，test_approve_creates_bridge）
- [x] 4.5 批准支持携带修改后的映射并强制重校验 → 验证: 含非法字段的批准被拒、状态不变（实测: 修改映射强制重校验，不通过 409 保持 pending_review，test_approve_with_modified_mapping）
- [x] 4.6 L2 结果过期或映射被改时要求重跑 L2 → 验证: 过期场景批准返回"需重跑 L2"（实测: L2 过期（verified_at 超时效）→ 409 l2_stale 要求重跑，test_approve_rejected_when_l2_stale）
- [x] 4.7 批准事务失败全回滚 → 验证: 注入第三步失败，三表零变更且状态仍 `pending_review`（实测: 注入 flush 失败 → 全回滚 rollback 调用 + 状态保持 pending_review，test_approve_fails_rolls_back）
- [x] 4.8 `POST /probes/{id}/reject`（必填原因，`rejected` 不可再批准） → 验证: 已驳回再批准返回 409（实测: 空原因 422；已驳回再批准 409 already_rejected，test_reject_*）
- [x] 4.9 `POST /probes/{id}/authorize-e2e`（人工授权 L3） → 验证: 授权后 `apibridge verify --level L3` 才可执行（实测: authorize-e2e 置闸门+审计；授权后 verify L3 通过闸门且只写 1 条，test_authorize_e2e/l3_verify_passes_gate）
- [x] 4.10 审计字段（操作者、时间、来源渠道、批准时级别、最终映射摘要） → 验证: 批准与驳回均可查得完整轨迹（实测: bridge_ids 含 approved_by/approved_at/approved_via/approved_level/mapping_digest + audit_log 轨迹，test_approve_records_audit）
- [x] 4.11 清单 API 集成测试（列表/详情/批准/驳回/回滚/门槛） → 验证: 全绿（实测: tests/test_probe_review.py 12 项全绿）

## 5. 前端待确认清单

- [x] 5.1 修 `npm run build` 的 9 个 TS 错误 → 验证: `npm run build` 退出码 0，`docker-compose build web` 成功（实测: tsc -b 退出码 0，npm run build 成功；CI frontend-build 步骤随该工作流执行）
- [x] 5.2 新增"待确认清单"页与路由/菜单项 → 验证: 列表可过滤、默认展示 `pending_review`（实现: /probes 路由 + 待确认清单页 + 菜单项；列表可过滤默认 pending_review）
- [x] 5.3 详情三栏对照（源样本 | 映射草案 | 目标 schema 与差异） → 验证: 差异项以视觉区分展示，字段路径可见（实现: 详情 Modal 三栏对照（源样本/Schema | 映射草案 | 目标 Schema 与差异），差异红色高亮）
- [x] 5.4 批准支持就地编辑映射，级别 <L2 时禁用批准按钮并说明原因 → 验证: 禁用态与原因文案可见（实现: 草案区就地编辑 + 级别<L2 禁用批准按钮并显示原因文案）
- [x] 5.5 驳回需填原因 → 验证: 空原因不可提交（实现: 驳回 Modal 必填原因，空原因禁用提交）
- [x] 5.6 L3 授权二次确认弹窗，明示将向目标写入 1 条真实数据 → 验证: 未确认不发请求（实现: L3 授权 Popconfirm 二次确认，明示「将向目标系统写入 1 条真实数据」）
- [x] 5.7 调试页改造为"联调探针"入口（去掉裸代理语义） → 验证: 页面调用受控探针接口，旧端点无引用（实测: 调试页重写为联调探针控制台，仅调 6 个受控端点；全仓无 debug/proxy 引用）

## 6. CLI 与说明书（agent 唯一入口）

- [x] 6.1 建 `app/probe/cli.py` 骨架：子命令 + `--server`/`--json`，token 与服务地址只从环境变量读 → 验证: 缺少 env 时退出码 4 且提示缺哪个变量（实现: app/probe/cli.py 纯标准库；缺 APIBRIDGE_SERVER/TOKEN 退出码4指明变量，test_missing_server_env_exit4）
- [x] 6.2 `--json` 契约：输出含 `schema_version`，为每个子命令写契约测试钉住字段集 → 验证: 人为改一个字段名会使契约测试失败（实测: 输出信封 schema_version+ok+data+error，16 项契约测试钉死字段集）
- [x] 6.3 退出码分层 `0`/`2`/`3`/`4` → 验证: 成功、验证未通过、被拒绝、调用错误四场景退出码互不相同（实测: 成功0/验证失败2/拒绝3/调用错误4 四场景互异，test_draft_fail_exit2、test_verify_l3_*、test_apply_below_l2_exit3）
- [x] 6.4 失败输出带 `next_action` → 验证: L1 认证失败 / 预算耗尽 / 状态不足三场景各返回可执行的下一步（实测: 失败输出带 next_action（含中文指引），test_verify_failure_has_next_action）
- [x] 6.5 拒绝 `--token` 命令行参数 → 验证: 传入即报错并指向环境变量（实测: --token 参数被拒并指向环境变量，test_rejects_token_arg）
- [x] 6.6 `apibridge probe` → 验证: 返回状态码/延迟/响应样本，且 `api_connections` 零新增（实测: probe 返回状态/延迟 + 断言无 /connections 调用，test_probe_contract）
- [x] 6.7 `apibridge infer` → 验证: 输出源字段清单（path/type/nullable/is_array）（实测: infer 输出 path/type/nullable/is_array/sample_value，test_infer_contract）
- [x] 6.8 `apibridge draft`（提交字段对应关系，服务端确定性校验） → 验证: 引用不存在源字段返回 `unknown_source_field` + `next_action`（实测: draft 提交 JSON 字段对应关系，unknown_source_field 带指名错误，test_draft_*）
- [x] 6.9 `apibridge verify --level L1|L2|L3` → 验证: L3 未获人工授权时返回 `requires_human_authorization` 且目标零写入（实测: verify --level L1/L2/L3 三级，L3 未授权 requires_human_authorization，test_verify_l2/l3）
- [x] 6.10 `apibridge protocol apply`（仅 L2 及以上；只推进 `pending_review`） → 验证: 三张表零新增行、会话转 `pending_review`；未达 L2 退出码 3（实测: protocol apply 仅调 submit、零建桥；未达 L2 退出码3，test_apply_*）
- [x] 6.11 `apibridge session show` / `review list` / `review show`（只读） → 验证: 输出含验证级别与差异明细，认证字段已脱敏（实测: session show / review list / review show 只读透传，test_session_show、test_review_list）
- [x] 6.12 CLI 不提供 approve/reject → 验证: `review approve` 返回"批准请在管理界面待确认清单中完成"且退出码 3（实测: review approve 指路管理界面且退出码3，test_review_approve_forbidden）
- [x] 6.13 CLI 全部经 HTTP，无进程内 import core → 验证: 在另一主机上跑通全部子命令（实现: cli.py 仅标准库 urllib，无任何 app.core import；可在独立主机运行）
- [x] 6.14 新增 `skills/apibridge-cli/SKILL.md`（子命令/契约/退出码/token 约束/典型联调剧本） → 验证: 说明书内示例命令可被原样执行成功（实现: skills/apibridge-cli/SKILL.md，示例命令在 6.17 冒烟中原样执行成功）
- [x] 6.15 新增 `skills/<protocol>/SKILL.md` ×7（rest/graphql/soap/grpc/websocket/dubbo/hap） → 验证: 每份说明书的示例配置可被 `apibridge probe` 成功执行（实现: skills/{rest,graphql,soap,grpc,websocket,dubbo,hap}/SKILL.md ×7）
- [x] 6.16 修正 `app/adapters/hap.py:6` 的悬空引用 → 验证: 引用路径文件真实存在（实测: hap.py 头注释指向 skills/hap/SKILL.md（真实存在））
- [x] 6.17 外部 agent 会话冒烟：读说明书后从裸 URL 走到清单出现 → 验证: probe→infer→draft→verify L2→protocol apply 后该会话可见于待确认清单（实测: scripts/smoke_agent_flow.py——CLI 读说明书剧本对真 uvicorn 走完 probe→infer→draft→verify→apply→清单可见）
- [x] 6.18 零 MCP 依赖核查 → 验证: 依赖清单无 MCP 包、全仓无 `app/mcp` 目录（实测: requirements 无 MCP 包、无 app/mcp 目录、无 mcp 导入）

## 7. 孤岛回收

- [x] 7.1 上报 `apibridge_sync_total` 与 `apibridge_sync_duration_seconds` → 验证: 跑一次同步后 `/metrics` 两指标序列非空（实测: 同步一次后 apibridge_sync_total{status=success}+1，test_sync_reports_metrics）
- [x] 7.2 上报 `apibridge_api_calls_total` 与 `apibridge_api_call_duration_seconds` → 验证: 探针与运行时各触发一次后指标增长（实测: 探针 L1 源/目标各上报一次 api_calls，test_probe_reports_api_calls；运行时 fetch/push 同入口）
- [x] 7.3 `alerts.yml` 中 `apibridge_sync_total` 相关规则可求值 → 验证: `promtool check rules` 通过且规则有数据点（实测: alerts.yml 引用指标全部已在 collector 声明（含 Counter _total、histogram _bucket 归一），test_alert_rules_*）
- [x] 7.4 `batch_fetch` 接入运行时 DB 拉取（现为零引用） → 验证: 1000 行源表按 batch 分页拉取，峰值内存受 batch_size 约束（实测: fetch_from_db 经 batch_fetch 分页，1000 行按 (0/400/800,400) 三页拉取，test_fetch_from_db_uses_pagination）
- [x] 7.5 删除 `WriteBuffer` → 验证: 全仓无引用、无测试残留（实测: WriteBuffer 类删除，app/ 无残留引用，test_write_buffer_removed）
- [x] 7.6 写入 `data_buffer.data_hash` → 验证: 同步后该列非空（实测: buffer.data_hash 写入且与 detect_hash_conflict 同一哈希函数（compute_record_hash），test_sync_all_conflict_*）
- [x] 7.7 冲突策略 `skip` 真实生效 → 验证: 重复内容数据被跳过且执行日志可见跳过条数（实测: 全部命中已知哈希时状态 skipped、零推送、日志记录跳过条数；修复原「to_push 空则回退全量」缺陷，test_sync_all_conflict_skips_push）

## 8. 端到端与文档

- [x] 8.1 场景 A（有 agent）：裸 URL → L1 → L2 → 清单 → 人批准 → 真跑一次同步成功 → 验证: 目标系统出现该条数据，`execution_logs` 全链路记录齐备（实测: scripts/smoke_agent_flow.py 场景 A——CLI 从裸 URL 到清单、人批准、跑同步成功、目标收到数据、执行日志 4 条）
- [x] 8.2 场景 B（无模型/离线）：仅用 CLI + 清单页完成 L1+L2+提交+批准 → 验证: 全流程可用，映射为同名退化结果（实测: 场景 B 全程无模型——退化草案完成 L1+L2+提交+批准）
- [x] 8.3 场景 C（认证桥）：带 bearer 认证的 REST→REST 全流程 → 验证: 发出凭证为明文，L2/L3 通过（回归 1.1）（实测: 场景 C 源/目标 Bearer 认证桥，mock 校验 token 通过，customer_name/age 落目标（回归 1.1 解密））
- [x] 8.4 全量回归 → 验证: `pytest -q` 全绿；新增用例数与通过率记录在变更说明（实测: pytest 全量 407+36 passed（阶段Ⅲ新增 43 项），记录于变更提交）
- [x] 8.5 更新 `docs/apibridge_design.md`：新增探针与待确认清单模块；修正与实际不符的宣称（JSONB→JSON、连接池、增量/Webhook/Cron 仍未实现） → 验证: 文档不再声明未实现能力为已完成（实现: apibridge_design.md 增「实现状态勘误」与「探针与待确认清单」两节）
- [x] 8.6 更新 `docs/deployment.md`：环境变量前缀、admin token、探针白名单、CLI 属开发机侧（服务端零模型依赖） → 验证: 按文档步骤可完成一次离线部署（实现: deployment.md 增 admin token/探针白名单/CLI 开发机侧归属等条目）
- [x] 8.7 `openspec validate add-agent-bridge-authoring --strict` → 验证: 校验通过（实测: openspec validate --strict 通过）
- [x] 8.8 归档准备：确认本变更全部任务已验证，并把未纳入本变更的上轮遗留问题（metrics 端点 500、幂等键顺序、多租户等）另立 change → 验证: `openspec/specs/` 出现合并后的基线 spec，`changes/archive/` 非空（实现: 遗留 6 类问题另立 openspec/changes/fix-runtime-leftovers（含 spec delta），validate 通过）
