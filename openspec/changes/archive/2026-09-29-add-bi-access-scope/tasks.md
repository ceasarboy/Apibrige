# add-bi-access-scope 任务清单

## 1. 模型与服务

- [x] `BiAccessProfile` 表（key/name/token_hash/token_prefix/enabled/boards/row_filters/
      hidden_fields/note/last_used_at）——验证：建表（启动 create_all）+ CRUD API 测试
- [x] `app/services/bi_access.py`：令牌生成/哈希/格式校验、`resolve_viewer` 依赖、
      范围纯函数（`row_filter_allows`/`scope_buckets`/`scope_defn`/`scope_rows`/
      `inject_row_filters`/`field_allowed`/`scope_metric_list`/`adhoc_hidden_field_used`）
      ——验证：纯函数单测 7 例
- [x] 只读白名单 + 默认拒绝——验证：API 身份矩阵（写端点/其它域端点/access 端点 403/401）

## 2. 接线

- [x] 中间件：`/api/v1/bi/` 且带 `X-BI-Token` 放行到路由（其余不变）——验证：手测 + 单测
- [x] **路由级身份依赖**（`APIRouter(dependencies=[Depends(get_viewer)])`）——写端点也过身份
      解析与白名单（**修掉了首版"写端点未挂依赖 → 带 BI 令牌可越权写"的漏洞**，
      由身份矩阵测试抓到）——验证：`test_profile_token_only_readonly_allowlist`
- [x] 看板范围（`/screens`、`/screen-settings`，含 cells 裁剪）——验证：API 测试 + UI 自审
- [x] 卡片范围（`/metrics` 列表 + `/metrics/{key}` 可见性校验 + 桶过滤 + 字段剥离）
      ——验证：API 测试 + 泄露测试
- [x] 即席（注入行过滤覆盖同名字段 + 隐藏字段 403 + 输出剥离）——验证：API 测试 + 真实数据
- [x] 字段树剥离 / 明细行过滤（内存分页分支，`fallback="row_permission"`）——验证：泄露测试
- [x] `detail` 卡例外（不走桶 → 按行过滤，不受"过滤字段须为维度"限制）——验证：单测 + 真实数据

## 3. 管理侧与投放页

- [x] 档案 CRUD API（仅管理令牌；响应永不回显 token_hash；明文仅创建/轮换一次）
      ——验证：API 测试
- [x] `/bi/access` 页（列表/新建/编辑/轮换/停用/删除/复制投放链接 + 令牌一次性展示）
      ——验证：tsc + 浏览器可达（管理页入口按钮）
- [x] `client.ts` 支持 `X-BI-Token`（未配管理令牌时）+ 投放页 `?token=` 注入并
      `history.replaceState` 抹除——验证：UI 自审（地址栏无 token、localStorage 有）

## 4. 验收

- [x] 单测 `tests/test_bi_access_scope.py` 20 项（纯函数 7 + 身份矩阵 4 + 范围强制/泄露 9）
      ——验证：全绿；全量 **1033 passed / 13 skipped**
- [x] 真实数据三路径对照 `scripts/access_scope_verify.py`（生产测试库，line=A线）
      ——验证：**全部通过**——桶 {"A线": 10577.0} == SQL 直查；明细 21 行全为 A线
      （SQL A线 21 / 全表 63）且隐藏列不出现、标 `row_permission`；即席客户端传
      `in[A线,B线]` 被授权值覆盖仍只剩 A线（引擎 pushdown）；隐藏字段 403；越权写 403
- [x] UI 自审 `scripts/access_scope_ui_check.py`（受限令牌投放页）——验证：**全部通过**
      （地址栏 token 已抹除；卡片级裁剪生效：line_output/line_pass 可见、output_trend
      等无产线维度卡片不出现；响应原文只含 A线 不含 B线/C线；无控制台报错）
- [x] tsc 零错误 + **六个** UI 自审脚本零回归——验证：命令输出
- [x] 文档（project-status / evolution-plan / skill / 指南 / AGENTS 陷阱）+ OpenSpec 归档
      ——验证：文档 diff

## 5. 实施中修正的两处（记录在案）

1. **写端点越权写漏洞**（首版）：中间件为"带 BI 令牌的 /bi 请求"放行到路由，但首版只给
   *只读端点* 挂了身份依赖 → `POST /bi/metrics` 等写端点**无鉴权直达处理器**。身份矩阵
   测试立刻抓到（期望 403 得到 400）。修法：改挂 **router 级依赖**（所有 /bi 端点统一过
   身份与白名单，默认拒绝），FastAPI 同请求内按依赖缓存 → 身份只解析一次。
2. **detail 卡被"过滤字段须为维度"误伤**：该规则源于"桶按维度组合存储"，而 detail 卡不走
   桶（实时读行后按行过滤）→ 加例外；即席同理（行过滤在聚合条件里生效）。
