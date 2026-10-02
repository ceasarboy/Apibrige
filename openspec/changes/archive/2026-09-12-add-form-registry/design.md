# Form Registry 设计

## Context

- 桥的本质是表单到表单，但现有模型（ApiConnection → FieldMapping）中"表单"只是映射上的技术字段（worksheet_id / source_table），无一等公民实体。
- 字段级注册已有先例 `ConnectionField`（connection_id + scope + path 三元唯一），表单级注册采用对称设计。
- 明道云 V2 开放 API 无"列工作表"接口；V3 有 `POST /v3/app/worksheets/list`（header `HAP-Appkey`/`HAP-Sign`，返回 `[{id, name, remark}]`）。**注意 V3 是 header 鉴权，与 V2 的 body 鉴权不同**。
- DB 侧列全部表的能力已存在（connectors/database.py：Oracle `user_tables`、MySQL `SHOW TABLES`、PG `information_schema`），字段抽屉在用。

## Goals / Non-Goals

**Goals**
1. 表单注册表：连接下可调用表单的登记（自动拉取 + 手动补充）、本地命名、备注。
2. 引用可见：每个表单"被哪些桥用了"（桥名、方向、对端），回答"哪些表单设置过"。
3. 映射创建联动：从注册表下拉选表单，替代手输 ID；保留手动兜底。
4. 无行为回归：注册表不参与同步执行链路；未登记表单的旧映射照常运行。

**Non-Goals**：见 proposal「非目标」。

## Decisions

### D1：独立 ConnectionForm 模型（否决 config JSON 方案）

名称/备注是用户长期维护的业务资产，需数据库级唯一约束与可索引查询；与 `ConnectionField` 架构对称。塞进 `ApiConnection.config` JSON 无约束、编辑回填/脱敏链路易碎。

### D2：form_key 的取值约定

| 连接协议 | form_key | 拉取来源 |
|---|---|---|
| hap | worksheet_id | V3 `worksheets/list` 的 `id` |
| oracle/mysql/postgresql/sqlite | 表名 | list_tables（保持源端大小写原样） |
| rest/soap/graphql 等 | 用户自定义（建议 endpoint 路径） | 无自动拉取，仅手动登记 |

匹配已有映射时**大小写互认**（Oracle 大写表名 × 映射小写，沿用 field_dict 的 `_lookup` 模式）。

### D3：拉取合并规则（幂等、保留用户资产）

- 新 form_key → 插入，`original_name=源端名`，`display_name=源端名`，`source=auto`；
- 已存在 → 仅刷新 `original_name`，**不覆盖 display_name/remark**；
- 源端已消失 → auto 项保留不删（防误删断桥）；
- 返回 `{added, refreshed, total}` 供前端提示。

### D4：桥引用统计（bridge_count / bridges）

在 `GET /forms` 中对 FieldMapping 全量计算（映射数量级小，无需缓存表）：

- HAP 侧匹配：`mapping_config.protocol.worksheet_id`（首选）或 `source_endpoint`/`target_endpoint` 与 form_key 相等（大小写互认）；
- DB 侧匹配：`source_table`/`target_table` 与 form_key 相等（大小写互认）；
- 每个引用项：`{mapping_id, mapping_name, side: "source"|"target", peer_connection_id, peer_connection_name, peer_form}`（peer_form 为对端的技术标识：对端 worksheet_id / 表名 / endpoint）。

### D5：HAP V3 list_worksheets 适配器方法

`HapV3Adapter.list_worksheets()`：`POST {base_url}/v3/app/worksheets/list`，header `HAP-Appkey: {appKey}` / `HAP-Sign: {sign}`，body `{"responseFormat": "json"}`（不传 worksheets 即返回全部）。凭证复用连接现有 appKey/sign（enc: 解密后注入，日志脱敏——与运行时同规范）。

**降级路径**：真实端点实测失败（版本不支持 V3 / 鉴权不过）→ sync 端点对该连接返回明确错误信息，前端提示改用手动登记；不阻塞其他功能。

### D6：映射页联动（前端）

- 源/目标连接选中后，表单字段渲染为 `Select`：选项 = `GET /forms?connection_id=`（显示 `display_name（form_key）`），另附"手动输入"选项切换回 `Input`；
- 选中表单 → 按协议回填：HAP 写 `mapping_config.protocol.worksheet_id` + endpoint；DB 写 `source_table`/`target_table`；
- 获取字段按钮逻辑不变（仍按现行 endpoint/table 参数拉取）。

## Risks

| 风险 | 缓解 |
|---|---|
| HAP V3 接口私有部署不可用 | 实现第一项即实测真实端点；失败降级手动登记（D5） |
| 大小写/ID 变体导致 bridge_count 漏统计 | 匹配统一走大小写互认；单测覆盖 Oracle 大写表名 × 小写映射用例 |
| REST 手动登记的 form_key 无语义校验 | 接受（登记即"用户认为可用"），错误 key 在建桥拉字段时自然暴露 |

## Migration

- 建表 `connection_forms` 随 `create_all` 自动创建，无迁移脚本。
- 存量映射不回填注册表（注册表是前端操作起点，不是同步依赖）；用户拉取/登记后引用统计自动关联上。

## Open Questions

无（设计已与用户对齐：独立模型、平铺+筛选、已建桥数与引用明细、映射页联动+手动兜底）。
