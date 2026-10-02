# add-bi-access-scope 提案（P6：看板数据权限——看板级 / 行过滤 / 列可见性）

> 演进计划中该期名为 P6「行列权限（复用 auth-management 域）」。调研后有一处**方案更正**：
> `auth-management` 域只覆盖**出站连接凭证**（API Key/OAuth/JWT…的加密存储与注入），
> 平台**没有用户/角色模型**（`app/security/auth.py` 注释明确"RBAC 是既有非目标"，
> 现在只有单一管理令牌）。因此本期不是"复用用户体系"，而是**新引入一个最小授权模型**；
> 名称相应改为 `add-bi-access-scope`（访问范围）。

## Why

现状：`?board=` 参数不构成鉴权（AGENTS §8 已明示），**任何持有管理令牌的人能看到全部看板、
全部行、全部字段**；而 BI 的真实使用场景里，"把这块大屏投给车间 / 投给供应商"是刚需——
车间只能看本车间的产量、供应商只能看自己的来料批次、成本列不该出现在对方大屏上。

要解决的是**数据可见范围**，不是"谁能登录"。因此本期不做用户/登录/口令体系
（那是另一个大域，且内网单管理员部署形态下收益低），而是引入**授权档案（Access Profile）**：
一份档案 = 一个只读令牌 + 一块可见范围（看板白名单 / 行过滤 / 列隐藏）。

## What Changes

1. **授权档案模型** `BiAccessProfile`（新表）：`key / name / token_hash(sha256，只存哈希) /
   token_prefix(前 8 位，列表识别用) / enabled / boards[] / row_filters{} / hidden_fields[] /
   note / last_used_at`。令牌明文**只在创建/轮换时展示一次**。
2. **BI 只读身份**：`/api/v1/bi/*` 接受两种身份——**管理令牌**（全权）或
   **`X-BI-Token`**（授权档案）。档案身份**只读**：仅下列端点放行，其余一律 403（默认拒绝）：
   `GET /bi/{dimensions,form-fields,form-rows,metrics,metrics/{key},screens,screen-settings}`、
   `POST /bi/adhoc/query`（设计上不落库）。
3. **服务端强制（不靠前端隐藏）**：
   - **看板范围**：`/bi/screens`、`/bi/screen-settings` 只返回授权内的看板；
   - **卡片范围**：`/bi/metrics` 只返回"在授权看板上 + 行过滤维度是卡片维度之一 +
     剥离隐藏字段后仍有可见度量"的卡片；
   - **行过滤**：读桶只保留 `dimension[field] == value` 的桶；明细在服务端过滤后再分页
     （标 `fallback="row_permission"`）；即席查询把授权行过滤**并进**定义（AND 语义，
     客户端无法放宽）；
   - **列可见性**：剥离 `hidden_fields`（度量值 / 维度键 / 明细列 / 字段树）；
     即席请求引用隐藏字段 → 403。
   - 不可见一律 **404 `{code:"not_visible"}`**（不泄露存在性）；403 只用于"越权写"与
     "请求了隐藏字段"。
4. **管理侧**：`/bi/access` 页——档案列表 / 新建 / 轮换令牌 / 停用 + 令牌一次性展示 +
   「复制投放链接」（`/bi/screen?board=<key>&token=<t>`）。
5. **投放页**：支持 `X-BI-Token`（URL `?token=` 首次注入 localStorage，之后读 localStorage）；
   受限身份下"看不到的看板/卡片根本不会返回"，无需前端隐藏逻辑。

## Impact

- **specs**：`bi-aggregation` 新增「BI 访问授权与数据范围」Requirement（含强制点与不泄露约定）
- **代码**：`app/models/bi.py`（新表）、`app/services/bi_access.py`（令牌校验 + 范围计算 +
  过滤/剥离纯函数）、`app/api/bi.py`（依赖注入 + 各端点接线）、`app/main.py`（bi 前缀放行
  给依赖校验）、`app/schemas.py`；前端 `api/client.ts`（BI token 支持）、
  `pages/bi/access.tsx`（新页）、`pages/bi/screen.tsx`（令牌注入）
- **不做（非目标，明确）**：
  - 用户 / 登录 / 口令 / 会话体系（RBAC 仍是非目标；本期是"只读令牌 + 范围"）
  - 写权限分级（档案身份只能读，写操作一律管理令牌）
  - 跨看板的**行级继承**（如"按部门层级"）——本期行过滤是**等值匹配**（可多字段 AND）
  - 字段级"脱敏"（只做"可见/不可见"，不做掩码）
  - 审计日志页（仅记 `last_used_at`）
- **验收**：
  1. **泄露测试**：受限令牌拉到的明细行**逐行满足**行过滤；聚合值 == 直接按该过滤口径算出的值；
  2. 身份矩阵：无效/停用令牌 401；写端点 403；未授权看板/卡片 404；隐藏字段请求 403；
  3. 真实数据端到端：生产测试库（DB 源）按 `line='A线'` 过滤——桶查询 / 明细 / 即席三路径
     数值与 SQL 直查一致（相对误差 1e-9）；
  4. UI 自审：投放页带受限令牌只显示授权看板与卡片、值已过滤、控制台无报错；
  5. 全量 pytest 零回归 + tsc 零错误 + 既有 5 个 UI 自审脚本零回归。
