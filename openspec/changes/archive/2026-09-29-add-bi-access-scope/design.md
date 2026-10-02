# add-bi-access-scope 技术设计

## 1. 身份与鉴权

```
请求 /api/v1/bi/...
├─ X-Admin-Token 命中        → Viewer(role=admin)    全权（含写）
└─ X-BI-Token 命中 + 档案启用 → Viewer(role=profile, profile=…)  只读 + 范围受限
   └─ 令牌不存在/已停用       → 401 {code:"bi_token_invalid"}
   └─ 未命中任何身份          → 401（中间件已拦）
```

**中间件改动**（`app/main.py`）：`/api/v1/bi/` 前缀且带 `X-BI-Token` 头时放行到路由，
真正校验落在**路由依赖**（需要 DB 会话读档案；中间件里读库代价高且层级不当）。
其余路径行为不变（仍只认管理令牌）。

**只读白名单**（`app/services/bi_access.py::READONLY_BI_ENDPOINTS`）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/bi/dimensions`、`/bi/form-fields`、`/bi/form-rows`、`/bi/metrics`、`/bi/metrics/{key}`、`/bi/screens`、`/bi/screen-settings` | 只读查询 |
| POST | `/bi/adhoc/query` | 只读语义（不落库，设计如此） |

实现：**默认拒绝**——依赖拿到 `request.method` + `request.url.path`，不在白名单即 403
（新增端点自动落入"仅管理令牌"，无需维护黑名单）。

## 2. 数据模型（`bi_access_profiles`）

| 列 | 说明 |
|---|---|
| `key` (pk, 32hex) | 档案 ID（列表/引用用，非机密） |
| `name` | 显示名（如"一车间大屏"） |
| `token_hash` | `sha256(token)` 十六进制；**库内不存明文**，明文只在创建/轮换响应里出现一次 |
| `token_prefix` | token 前 8 位（列表识别与人工核对） |
| `enabled` | 停用即 401（每次请求实时查库，不缓存 → 停用立即生效） |
| `boards` | 可见看板 key 列表；**空数组 = 不限制看板**（admin 建档案时的显式选择，UI 标"全部看板"） |
| `row_filters` | `{字段: 值}`，多字段 AND；字段口径 = 定义里的维度 field / 明细列名 |
| `hidden_fields` | 度量 key / 维度 field / 明细列 path |
| `note` / `last_used_at` | 备注 / 最近使用（可观测，不记审计流） |

令牌格式 `bit_<32 hex>`，`secrets.token_hex(16)` 生成；校验用 `secrets.compare_digest`
（与既有管理令牌同款恒定时间比较）。

## 3. 范围计算（纯函数，便于单测）

```python
# app/services/bi_access.py
def row_filter_allows(defn, profile) -> tuple[bool, str]
    """行过滤是否可安全施加：过滤字段必须是卡片维度（否则聚合层无法隔离行）"""

def scope_buckets(buckets, defn, profile) -> list[dict]
    """读桶范围：按维度等值过滤 + 剥离隐藏度量/维度"""

def scope_defn(defn, profile) -> dict
    """剥离隐藏度量/维度（用于 /bi/metrics 列表与响应）"""

def inject_row_filters(payload, profile) -> dict
    """即席：把授权行过滤并进请求 filters（授权侧 filters 恒定保留，客户端无法放宽）"""

def scope_rows(rows, profile) -> list[dict]
    """明细：行过滤 + 剥离隐藏列"""

def field_allowed(field, profile) -> bool
```

**关键安全规则**：
1. 行过滤字段**必须**是卡片的维度之一 —— 否则该卡对该档案**不可见**（404）。
   理由：桶是按维度组合存的，若卡片不按该维度分组，平台无法在桶层隔离行；退而"临时重算"
   代价高且易错，宁可不可见（宁可少给，不可多漏）。
2. 即席查询：授权 `row_filters` 作为**前置条件**注入（同名维度字段以授权值为准，
   客户端传入的同字段过滤被替换而非叠加——避免 `A线 OR B线` 放宽范围）。
3. 隐藏字段若被即席请求引用 → 403 `hidden_field`（明确告知，便于集成方排查）。
4. 明细行的隐藏列在**服务端剥离**（响应里不存在该键），不是前端不显示。

## 4. 各端点接线

| 端点 | 施加 |
|---|---|
| `GET /bi/screens` | boards 非空 → 只返回授权看板 |
| `GET /bi/screen-settings?board=` | 同上；board 不在范围 → 404 `not_visible` |
| `GET /bi/metrics` | 限授权看板上的卡片 + 行过滤维度校验 + 剥离隐藏字段后仍有用度量 |
| `GET /bi/metrics/{key}` | 同上校验（404/403）→ 读桶 → `scope_buckets` |
| `POST /bi/adhoc/query` | 注入行过滤 + 隐藏字段引用 403；输出剥离隐藏字段 |
| `GET /bi/form-fields` | 剥离隐藏字段 |
| `GET /bi/form-rows` | 行过滤（走内存分支，标 `fallback="row_permission"`）+ 剥离隐藏列 |

`GET /bi/form-rows` 的**分页正确性**：过滤必须发生在分页之前 → 对受限身份强制走
"取缓存全量行 → 过滤 → 切片"分支（与既有 `keyword` 降级路径同构，复用同一处代码）。

## 5. 前端

- `api/client.ts`：`getStoredToken()` 增加 BI 回退——未配置管理令牌时用
  `localStorage['apibridge_bi_token']` 作为 `X-BI-Token`（投放页专用；管理页仍用管理令牌）。
- `pages/bi/screen.tsx`：进入时若 URL 带 `?token=` → 写入 localStorage 并**从地址栏移除**
  （`history.replaceState`，避免被截图/转发带出）。
- `pages/bi/access.tsx`：档案 CRUD + 令牌一次性展示 + 复制投放链接。
- 受限身份下**不需要**前端隐藏逻辑：范围由服务端裁剪（这是本设计的安全前提）。

## 6. 测试

| 层 | 用例 |
|---|---|
| 单测（纯函数） | 行过滤字段缺失 → 不可见；桶过滤只留匹配组合；隐藏字段剥离；即席注入**覆盖**同名字段；明细行过滤 + 列剥离 |
| API | 身份矩阵（admin / 有效档案 / 无效 / 停用）；只读白名单（写端点 403）；看板范围；未授权卡片 404；隐藏字段 403；令牌轮换后旧令牌 401 |
| **泄露测试** | 受限令牌：明细行逐行满足过滤；桶值 == 按过滤口径直算值；`form-fields`/`metrics` 不含隐藏字段 |
| 真实数据 | 生产测试库（DB 源）`line='A线'`：桶/明细/即席三路径 vs SQL 直查（1e-9） |
| UI 自审 | `scripts/access_scope_ui_check.py`：投放页带受限令牌 → 只显示授权看板/卡片、值已过滤、无控制台报错 |

## 7. 风险与处置

| 风险 | 处置 |
|---|---|
| 令牌经 URL 泄露（浏览器历史/代理日志） | 前端取用后立即 `history.replaceState` 抹掉；文档写明"仅首次投屏用，长期运行请用 localStorage"；令牌可一键轮换 |
| 管理令牌与 BI 令牌混用导致越权 | 档案身份**默认拒绝**所有白名单外端点（含写）；单测覆盖"用 BI 令牌调写端点 403" |
| 行过滤维度缺失的卡片"看起来没了" | 属设计（宁可少给）；`GET /bi/metrics` 响应带 `scope: {hidden_metrics: [...]}` 让管理员在管理侧可见原因（受限身份不下发该字段） |
| 停用/轮换生效延迟 | 每次请求实时查库（唯一索引命中，代价小），无缓存 → 立即生效 |
| 令牌哈希被彩虹表反推 | 令牌 32 hex 随机（128 bit 熵），sha256 足够；库内无明文 |

## 8. 落地记录（2026-09-29 实施）

提交：`feat(P6)`（模型 + 服务 + 接线 + 前端 + 单测 20 + 真实验证 + UI 自审）、`docs` 收口。

### 8.1 实施中修正的两处

1. **写端点越权写（安全漏洞，首版即有）**：中间件把"带 `X-BI-Token` 的 `/api/v1/bi/*`"
   整体放行到路由，而首版只在只读端点挂身份依赖 → `POST /bi/metrics`、`POST /bi/aggregate/run`
   等**无鉴权直达处理器**。身份矩阵测试（期望 403、实得 400）当场抓到。修法：`APIRouter`
   级依赖 `dependencies=[Depends(get_viewer)]` → 所有 /bi 端点（含写、含将来新增的）
   统一过身份解析与只读白名单；FastAPI 同请求内按依赖缓存，身份只解析一次。
   **教训**：新增"身份层"必须挂在**容器级**（router/app），端点级挂法天然漏掉后来新增的端点。
2. **detail 卡与即席的例外**：`row_filter_allows` 的"过滤字段须为维度"约束源于"桶按维度组合
   存储"；detail 卡不走桶（实时读行后 `scope_rows` 过滤）、即席把过滤并进聚合条件 →
   二者不受该约束（否则"只有明细没有维度的卡"对任何行过滤档案都不可见）。

### 8.2 真实验证（scripts/access_scope_verify.py，生产测试库，全部通过）

| 路径 | 结果 |
|---|---|
| 预聚合桶（line_output） | 受限视角 `{"A线": 10577.0}` == SQL 直查；全表 30848.0 未泄露 |
| 明细分页 | total 21 = SQL `line=A线` 计数（全表 63）；每行 line=A线；隐藏列不在响应；标 `row_permission` |
| 即席 | 客户端传 `in[A线,B线]` 试图放宽 → 结果仍只剩 A线（引擎 pushdown），数值一致 |
| 隐藏字段引用 | 403 `hidden_field` |
| 越权写 | 触发聚合 403 / 建授权 403 |

### 8.3 UI 自审（scripts/access_scope_ui_check.py，全部通过）

带 `?token=` 打开投放页：地址栏 token 被抹除（`history.replaceState`）、令牌入 localStorage；
**卡片级裁剪在浏览器可见**——`line_output`/`line_pass`（含产线维度）渲染，
`output_trend`（仅日期维度）等卡片不出现；页面自发的取值响应原文只含「A线」、
不含「B线/C线」；控制台无报错。

### 8.4 已知边界（设计取舍，写进文档）

- **行过滤维度缺失的卡片不可见**（宁可少给，不可多漏）；管理员在授权页看到的是
  "这份授权下能看到哪些"（受限身份看不到原因，避免泄露）。
- 档案身份**只读**：写操作一律需要管理令牌。
- 令牌经 URL 传入仅用于首次投屏；长期运行读 localStorage；泄露可用「轮换」一键失效。
- 行过滤是**等值匹配**（多字段 AND）；层级/范围语义留给后续变更。
- 列隐藏是"不可见"而非"脱敏"（值不在响应里，不是打码）。
