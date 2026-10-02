## Why

新建一条数据桥接（选协议 → 填连接 → 配认证 → 试接口 → 定字段映射）目前完全靠人肉联调，这是本平台最耗时、最高错误率的一环。同时，本变更要修掉一个让这个环节无法自动化的根因：**探针路径与运行时路径不是同一份代码**。

现状实测三处硬阻塞：

1. `app/services/api_client.py:33-34` 的 `build_headers` 直接用加密态的 `conn.auth_config`，全项目只有 `app/api/connections.py:156`（测试连接）调过 `decrypt_auth_config`。后果：任何带认证的源/目标，运行时发出的是 `Authorization: Bearer enc:gAAAA...`，必然 401。**LLM 再聪明也只能对着一堵 401 墙。**
2. `app/adapters/` 七个协议适配器在生产代码里唯一调用点是"测试连接"按钮（`connections.py:165`），同步引擎走的是只懂 REST 的 `api_client.py`。`protocol-adapters/spec.md:9` 要求「核心同步逻辑 MUST 通过该接口调用」未被满足。后果：探针说"通了"和任务"能跑"是两件事。
3. `app/api/debug.py` 的 `/proxy` 与 `/quick-proxy` 是无鉴权、无目标限制、无响应体上限的全回显转发。它已经是本项目最高危入口，不能被 agent 复用，只能被收编。

本变更的目标不是"再加一个 AI 按钮"，而是把**"联调通过"从一个形容词变成一个可验证的状态**。

## What Changes

### 新增：探针内核（`app/probe/`）
- **三级验证定义**，取代含糊的"通了"：
  - `L1 reachable` — 端点可达、认证有效（不搬运业务数据）
  - `L2 transform_verified` — 真实拉取源数据 + 真实转换 + 真实校验 + 目标端契约比对，**不写入目标**
  - `L3 e2e_verified` — 人工授权后写入 1 条并回读，用 `app/testing/comparator.py` 比对一致
  - 只有 L3 才叫"同步真的通了"；达到 L2 才允许进入待确认清单
- **Schema 推断**：从源响应样本与目标表结构双向推出字段清单（路径/类型/可空/数组），喂给映射草案
- **映射草案生成**：agent 提议字段对应关系，core 用两端 schema 做确定性校验；无模型可用时退化为同名匹配，仍工作
- **ProbeSession**：一次联调的全部产物与状态，落 PostgreSQL（真相源原则一致）

### 新增：待确认清单（人审后才落库）
- `probe_sessions` 表 + 状态机：`draft → probing → L1_reachable → transform_verified → pending_review → approved → committed`，任一阶段可转 `failed`，人可 `rejected`，超期自动 `expired`
- API：列表 / 详情 / 批准（允许批准时修改映射）/ 驳回
- 批准动作在**单个事务**内创建 `ApiConnection`（若为新）+ `FieldMapping` + `SyncTask`
- 前端新增"待确认清单"页：三栏对照（源样本 | 映射草案 | 目标样本与差异），批准/驳回/重跑

### 新增：Agent 接口（CLI 为唯一入口，服务端零模型）
- **agent 只调一个东西：`apibridge` CLI**。pi agent / Claude Code / Trae 这类外部工具读 `skills/apibridge-cli/SKILL.md` 学会用法，通过 shell 驱动 CLI 完成整个联调循环
- **CLI 子命令**：`probe`（发一次真实探测）/ `infer`（源端字段清单）/ `draft`（提交字段对应关系并回校验结果）/ `verify`（跑 L1、L2）/ `protocol apply`（**将协议草案提交至待确认清单**）/ `session show` / `review list|show`
- **agent-readable 硬要求**：`--json` 是版本化的稳定契约；退出码区分成功 / 验证失败 / 被拒绝 / 调用错误；每个失败带 `next_action` 提示下一步
- **本变更不做 MCP server**：CLI 已覆盖 agent 需求，MCP 作为后续可选 facade（工具面与探针契约不变，届时只需加一层薄封装）
- 协议说明书：新增 `skills/<protocol>/SKILL.md`（rest/graphql/soap/grpc/websocket/dubbo/hap）+ `skills/apibridge-cli/SKILL.md`，并补上 `app/adapters/hap.py:6` 现在指向不存在文件的悬空引用

### 新增：安全收口（`probe-security`）
- 管理 API 最小共享 token 鉴权（`APIBRIDGE_ADMIN_TOKEN`，`/health` 与 `/metrics` 豁免）
- 探针目标白名单：默认拒绝回环/RFC1918/链路本地/云元数据地址，需显式 allowlist；重定向逐跳复校验；响应体大小上限
- `debug/proxy` + `debug/quick-proxy` **降级为探针内核的实现底座并删除裸端点**

### 修复：本功能成立的前置（原 `add-management-ui-and-protocols` 漏项）
- 同步链路读取凭据前解密；密钥缺失时明确失败而非 500；PUT 回传掩码值 `***` 时保留原值不覆盖
- `route_transfer` 的 `_fetch_source`/`_push_target` 真正走 `ProtocolAdapterFactory`
- `docker-compose.yml` 环境变量补 `APIBRIDGE_` 前缀（`config.py:6` 的 `env_prefix` 使当前写法全部无效），同步修正 `docs/deployment.md` 环境变量表
- `scripts/init.sql` 由 ORM 模型生成（现缺 `field_mappings.source_headers/target_headers`、`sync_tasks.depends_on_task_id/conflict_strategy`），并加一致性检查

### 回收：三处孤岛（探针正好是它们的使用者）
- `metrics_collector.py` 九个指标中至少 `sync_counter`/`sync_duration`/`api_calls`/`api_call_duration` 接入真实上报 — 让 `configs/alerts.yml` 4 条哑火规则与 Grafana 面板复活
- `connectors/batch.py` 的 `batch_fetch` 接入探针分页拉取；删除 `WriteBuffer`（零引用且 `_timeout_flush` 自取消会导致批次丢失）
- `data_buffer.data_hash` 补写入（现被查询、有索引，但全项目无一处写入 → 冲突检测恒空转）

## 技术选型权衡分析

| 决策点 | 选项 | 选择 | 理由 |
|--------|------|------|------|
| 模型归属 | 平台内置调模型 / 驱动侧 agent 持有 | **驱动侧 agent 持有** | APIBridge 服务端不引入任何模型依赖，内网离线包与生产镜像不变；将来若要内置可原样复用探针内核 |
| Agent 入口 | MCP server / CLI / 内置按钮 | **CLI 单一入口** | pi/Claude Code 本来就能用 shell 驱动 CLI，零新增协议、零新进程、零依赖；agent 需要的只是结构化的 `--json` 输出与一份说明书 |
| MCP 是否本期做 | 本期 / 推后 | **推后** | CLI 已覆盖 agent 需求；探针内核与工具契约不变，将来加 MCP 只是薄封装，不预付复杂度 |
| agent 写库权限 | 可直接 CRUD / 必须经人批准 | **必须经 ProbeSession + 人工批准** | 桥接会写真实外部系统，误写不可逆；清单同时成为审计记录 |
| "通了"的定义 | 单一布尔 / 三级验证 | **L1/L2/L3 三级** | 布尔会给出假信号（探针通≠同步通），三级各自可独立复现 |
| 探针走哪条代码 | 新写探针 / 复用运行时同一条 | **强制复用 `ProtocolAdapterFactory` + 真实 handler** | 否则"L2 通过"毫无意义，这是本变更的立身之本 |
| dry-run 是否写目标 | 真写 1 条 / 只读回环 / 契约级验证不写 | **L2 不写，L3 才写且需人工授权** | 未确认前向生产系统写数据不可接受 |
| 无模型时能否用 | 不能 / 退化为同名匹配 | **退化可用** | 内网无端点或离线场景仍能跑 L1/L2，核心不依赖 LLM |
| 鉴权形态 | 共享 token / 完整 RBAC | **共享 token** | 单租户内部工具，RBAC 是既有非目标；先堵 SSRF 与裸写库 |
| init.sql 漂移 | 引入 Alembic / 从模型生成 + 一致性检查 | **从模型生成 + CI 检查** | Alembic 是新运行时依赖且属独立议题，不夹带；先止血 |
| 映射对应关系来源 | LLM 直接产出即用 / LLM 提议 + core 校验 | **提议 + 确定性校验** | 校验是代码，不是模型判断，防止幻觉字段进生产 |

## 非目标

- **不做平台内置 LLM 调用**（"AI 调试"按钮）：模型地址与 key 只存在于工程师本机 agent 的配置里，服务端零模型依赖
- **不做 MCP server**：本期只交付 CLI facade；探针内核与工具契约与宿主无关，将来可无改动新增
- **不做对话式 agent UI**：待确认清单是普通表格页，不做聊天面板
- **不做多租户、RBAC、计费**：沿用既有非目标，本期只有单共享 token
- **不做映射智能推荐产品化**：agent 提议 + core 校验即止，不自建推荐算法、不做相似度学习、不存训练数据
- **不做批量重放/大数据量压测自动化**：探针只跑小样本（默认 ≤20 条）；压测归 `app/testing/stress.py`
- **不引入 Alembic 或任何迁移框架**：仅用"从模型生成 init.sql + 一致性检查"止血
- **不改造前端为对话式 Agent UI**：保留原有义——待确认清单页不做聊天面板
- **不实现 WebSocket 持久订阅式源**：沿用既有约束（拉取后断开）
- **不引入 MCP 依赖**：本期无任何 MCP 相关依赖，生产镜像与离线包均不变

## Capabilities

### New Capabilities

- `probe-toolkit`: 探针内核 — 三级验证（L1/L2/L3）、schema 推断、映射草案确定性校验、ProbeSession 状态机
- `bridge-review-queue`: 待确认清单 — 人工查看/编辑/批准后原子创建连接、映射、任务；未批准不落库；全程可审计
- `agent-tool-interface`: Agent 接口 — CLI 为唯一 agent 入口，agent-readable 输出契约（版本化 JSON + 退出码 + 下一步提示），协议与 CLI 使用说明书
- `probe-security`: 探针与管理 API 安全边界 — 共享 token 鉴权、目标地址白名单与私网拒绝、响应体上限、`debug/proxy` 收编删除

### Modified Capabilities

- `data-pipeline`: 同步/异步处理器读取凭据前必须解密；`data_hash` 必须写入使冲突检测生效；批量预取接入；传输层指标必须上报
- `protocol-adapters`: 补齐 `add-management-ui-and-protocols` 未兑现的要求 — 核心同步逻辑 MUST 通过适配器接口调用（消除 `api_client.py` 平行路径）

## Impact

- **新增代码**：`app/probe/`（prober/schema_infer/mapping_draft/verifier/session/cli）、`app/api/probes.py`、`frontend/src/pages/probes/`、`skills/<protocol>/SKILL.md` ×7、`skills/apibridge-cli/SKILL.md`
- **删除代码**：`app/api/debug.py` 两个裸端点、`app/connectors/batch.py::WriteBuffer`
- **数据模型**：新增 `probe_sessions` 表；`data_buffer.data_hash` 开始被写入；`init.sql` 由模型生成
- **配置**：新增 `APIBRIDGE_ADMIN_TOKEN`、`APIBRIDGE_PROBE_ALLOWED_HOSTS`、`APIBRIDGE_PROBE_MAX_BODY_BYTES`；compose 环境变量补前缀
- **破坏性变更**：`/api/v1/*` 全部需要 token（前端加 token 输入与拦截器）；`/api/v1/debug/proxy`、`/api/v1/debug/quick-proxy` 移除
- **性能影响**：L2 探针每次 ≤20 条源数据，写放大限于 `probe_sessions` 一行；L3 每次 1 条真写
- **测试影响**：现有 307 例中依赖 `debug/proxy` 与 mock `api_client` 的用例需迁移到适配器路径
- **部署影响**：内网离线包与生产镜像**完全不变**（不新增任何运行时依赖，无 MCP 包）；前端需重新构建（顺带修 `npm run build` 的 9 个 TS 错误，否则 `docker-compose build web` 仍失败）
