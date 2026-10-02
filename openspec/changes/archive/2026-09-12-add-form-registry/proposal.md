## Why

平台的桥（同步链路）本质是**表单到表单**的，但现有界面只有两个层级：

1. 连接管理——地址到地址（HAP 应用 / 数据库实例）；
2. 映射配置——手动输入 worksheet_id / 表名查询字段并设置。

**缺失中间层：连接下"可调用的表单"没有注册表。** 导致：

1. 不知道一个连接下有哪些表单可用（必须去明道云/数据库里翻）；
2. 不知道哪些表单已经建过桥、被哪些桥引用（映射页按 worksheet_id 手输，无表单视角）；
3. 表单没有本地名称与备注，worksheet_id/表名是技术标识，无法承载业务语义（"这是什么表单"）。

只有先知道"哪些表单可用"，才能建立桥连接两个表单。

## What Changes

### 新增：表单注册表（ConnectionForm）

- 新增 ORM 模型 `ConnectionForm`（建表 `connection_forms`，随 `create_all` 自动创建）：
  - `connection_id` + `form_key` 二元唯一（HAP=worksheet_id；DB=表名；REST 手动登记=用户自定义标识）
  - `original_name`（源端原始名，拉取时写入）/ `display_name`（本地显示名，用户可改，默认=original_name）
  - `remark`（备注：说明这是什么表单）、`source`（`auto` 拉取 / `manual` 手动补充）
- 注册表从属于连接，与字段字典（`ConnectionField`，字段级）平行的表单级注册。

### 新增：表单注册表 API（`app/api/forms.py`）

- `GET /api/v1/forms?connection_id=`（连接筛选可空=全部）：
  每行含 display_name / original_name / form_key / remark / source / 所属连接信息，
  以及 **`bridge_count`（已建桥数）与 `bridges` 引用明细**（桥名称、本表单方向 源/目标、对端连接名与对端表单标识）。
- `POST /api/v1/forms/{connection_id}/sync` — 从源端拉取表单清单合并入注册表：
  - DB 连接：复用现有 list_tables 能力（Oracle user_tables / MySQL SHOW TABLES / PG information_schema）；
  - HAP 连接：新增 V3 适配器方法调 `POST /v3/app/worksheets/list`（header `HAP-Appkey`/`HAP-Sign` 鉴权，返回 `[{id, name, remark}]`）；
  - REST 等无结构协议：返回 400 提示不支持自动拉取，走手动登记。
  - 合并规则：新 form_key 插入（source=auto）；已存在的不覆盖 display_name/remark（保留用户设置），仅刷新 original_name；
    源端已消失的 auto 项**保留**（防误删，防断桥）。
- `POST /api/v1/forms` — 手动登记（connection_id + form_key + display_name + remark，source=manual）
- `PUT /api/v1/forms/{id}` — 修改 display_name / remark
- `DELETE /api/v1/forms/{id}` — 删除（仅清理注册信息，不影响已建桥）

### 新增：前端"表单管理"独立页

- 菜单新增"表单管理"（与连接/映射/任务平级）。
- 平铺表格 + 顶部筛选：连接下拉（可空=全部）、名称/备注搜索框。
- 列：本地名称 / 原始名 / form_key / 所属连接 / 备注 / 来源（auto/manual 标签）/ **已建桥数**（悬停显示引用桥明细：桥名、方向、对端连接+对端表单）/ 操作（编辑、删除）。
- 「从源端拉取」按钮（按当前筛选连接触发 sync）+「手动添加」按钮（弹窗填 form_key/名称/备注）。

### 修改：映射创建页表单选择联动

- 选择源/目标连接后，表单字段从**该连接的注册表下拉选择**，显示 `display_name（form_key）`；
- 选中后自动填充对应的 worksheet_id / 表名（mapping_config 与 source/target_table 按现行约定）；
- 保留"手动输入"开关兜底：注册表未登记的表单仍可直接输入 ID/表名，旧流程不回归。

## 非目标

- 不做表单级同步行为变更（注册表是登记/命名/引用统计，不参与同步执行链路）。
- 不做 HAP 分组（section）层级展示（worksheets/list 返回平铺清单，V3 无分组信息时保持平铺）。
- 不做 REST 连接的自动表单发现（无结构协议，仅手动登记）。
- 不做"源端已消失表单"的自动清理或标记管理（本轮保留不删，后续按需）。
- 不改探针 CLI（agent 建桥仍走 pending_review 流程，注册表是管理界面能力）。

## Capabilities

### Modified Capabilities

- `connection-api`: 新增表单注册表 API（列表含引用统计 / 拉取合并 / 手动登记 / 编辑 / 删除）
- `management-ui`: 新增表单管理页；映射创建页表单选择改为注册表下拉 + 手动兜底

## Impact

### 代码影响

- **新增** `ConnectionForm` 模型（app/models/sync.py）
- **新增** `app/api/forms.py` — 注册表 CRUD + sync 拉取端点（router 在 main.py 注册）
- **新增** `app/services/form_registry.py` — 拉取合并逻辑 + 桥引用统计（FieldMatching：HAP 按 worksheet_id/endpoint、DB 按 source/target_table，大小写互认）
- **修改** `app/adapters/hap.py` — 新增 `list_worksheets()`（V3 worksheets/list，header 鉴权）
- **修改** `frontend/src/pages/` — 新增 forms 页面 + 路由/菜单；映射页表单下拉联动
- **新增** `tests/test_form_registry.py` — API/合并/引用统计测试（HAP 拉取 mock + DB 拉取走现有连接器）

### 依赖变更

无新依赖（V3 HTTP 调用走适配器现有 http 客户端）。

### 部署影响

无（建表随启动 create_all 自动完成）。

### 风险

- **HAP V3 `worksheets/list` 在用户私有部署环境的可用性**：实现第一步即真实端点实测（项目经验 18：改鉴权/请求方式必须实测真实端点）；不可用则 HAP 拉取降级为"手动登记 + 按 ID 校验"，其余功能不受影响。
