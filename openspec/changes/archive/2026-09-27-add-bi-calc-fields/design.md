# add-bi-calc-fields 技术设计

## 1. 表达式引擎（派生列）

**语法**（借用 DataEase 的 `[引用]` 惯例，方括号包裹）：

```
[合格数] / [检验数] * 100
round([实际产量] / [计划产量] * 100, 1)
coalesce([返工数], 0) + [合格数]
```

- 引用优先按**中文显示名**解析（取字段字典/表单字段元数据），回落**行键（path）**；
  两者都不命中 → 校验期报错（不静默产出 null 列）
- 运算符：`+ - * / %`、括号、数值字面量；比较与逻辑本轮不做
- 函数（白名单 7 个）：`round(x[, d]) abs ceil floor min(a,b,..) max(a,b,..) coalesce(a,b,..)`
- **求值实现**：`ast.parse(expr, mode="eval")` + 白名单节点遍历求值
  （BinOp/UnaryOp/Call/Constant/Name→引用表），**禁止** `eval` 直接执行；
  引用在求值前替换为行的数值
- **校验期静态检查**：AST 只含白名单节点/函数/引用；引用字段存在；
  派生列之间按引用关系拓扑排序，**循环引用报 422**（DataEase 递归上限 100 的等价防护）
- 求值容错：单行缺值/不可转数 → 该行派生值为 null（该行在 agg 时被跳过，与现有
  `compute_measure` 跳过不可转数值行的语义一致）

**定义结构**：

```json
"computed": [
  {"key": "比率", "name": "合格率", "expr": "[合格数] / [检验数] * 100", "field_type": "number"}
]
```

派生列可被 `dimensions[].field` 与 `measures[].field` 引用（用 `key` 或 `name`）。

## 2. 聚合比（ratio 算子）

```json
{"key": "整体达成率", "agg": "ratio",
 "numerator": {"field": "actual_qty", "agg": "sum"},
 "denominator": {"field": "plan_qty", "agg": "sum"},
 "scale": 100}
```

- 语义：先分别聚合分子分母再相除（**不是**行级比率的平均——那是 `computed`+`avg`）
- 除零/空 → null（渲染层显示 "—"，不编造 0）
- 分子分母也可引用派生列 key

## 3. 自定义分组（维度值归一）

```json
{"name": "车间", "field": "line",
 "groups": [{"name": "一车间", "values": ["A线", "B线"]},
            {"name": "二车间", "values": ["C线"]}],
 "other": "其他"}
```

- 聚合前对行的维度值查映射：命中组名 → 组名；未命中 → `other`（缺省原值不归并）
- 与 Top N 的「其他」项共存时：Top N 归并名为「其他」——文档提示潜在同名（校验期
  若 other 与 Top N 标签同名给出 warning，不阻断）

## 4. 展示层派生（bi_format 修饰）

- **占比**：`format` 已有 datav-number 与 echarts-pie；饼图占比由 ECharts 计算显示，
  柱状/表格占比需在转换器加 `show_percent`（值/总和×100，保留 1 位）
- **累计**：时序（timeseries/category）加 `cumulative: true`——按 x 顺序 cumsum；
  快照源多桶即"当前状态的累计视图"（同一份数据累计会失真，**仅滚动源启用**，
  快照源校验期拒绝并提示）
- 两者都是**修饰开关**（定义顶层 `display: {percent?: bool, cumulative?: bool}`），
  不改聚合桶——历史桶重算不需要

## 5. 测试与验证

- 单测：表达式解析（白名单/循环引用/引用解析优先级/容错）、ratio 算子（除零）、
  分组归一（命中/未命中）、占比/累计转换器
- 端点测试：带 computed/ratio/groups 的定义 CRUD + 查询
- 真实链路：生产测试库上加 3 个示例（整体达成率 ratio / 合格率派生列 + 分组维度 /
  占比柱状），数值与 SQL 直查对照；Playwright 看板验收
- 自审脚本：`scripts/bi_calc_fields_smoke.py`（新建，数值对照 + 定义回读）

## 6. 交付切分（拍板后）

1. 后端：表达式引擎 + computed + ratio + groups（校验/聚合/端点透传）+ 测试
2. 前端：定义页计算字段编辑器 + 分组配置
3. 展示层：占比/累计 + 看板验收 + 文档（guide/skill 更新）
