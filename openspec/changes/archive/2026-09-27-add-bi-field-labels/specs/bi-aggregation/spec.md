# add-bi-field-labels 规格增量

## ADDED Requirements

### Requirement: 字段显示别名

系统 SHALL 支持指标定义级的字段显示别名（`style.column_labels`），
显示名优先级为「卡级别名 > 字段字典 name > 原始字段名」。

#### Scenario: 明细表头显示别名

- **WHEN** 明细指标定义声明 `style.column_labels = {"work_date": "生产时间"}`
- **THEN** 明细表头与排序字段下拉 MUST 显示"生产时间"（原始值仍以 `work_date` 传输）
- **AND** 未声明别名的字段 MUST 回落字段字典 name，字典未配置时回落原始字段名

#### Scenario: 定义页编辑入口

- **WHEN** 用户在定义页编辑明细图表（form 源）
- **THEN** MUST 提供「列显示名」编辑区（逐字段输入别名，留空即回落）
- **AND** 保存后 MUST 经 `style.column_labels` 随定义持久化，投放页与放大弹层
  一并生效

#### Scenario: 与全局字段字典的关系

- **WHEN** 同一字段在字段字典（连接级）与卡片别名同时配置
- **THEN** 卡片别名 MUST 优先（局部覆盖全局），且两者互不影响
