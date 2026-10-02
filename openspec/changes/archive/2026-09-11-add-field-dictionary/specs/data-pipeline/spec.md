## ADDED Requirements

### Requirement: 按连接字段字典进行读写过滤

系统 SHALL 在同步传输中按连接字段字典过滤字段：源连接字段字典中 `readable=false` 的字段不参与读取与删除比对，目标连接字段字典中 `writable=false` 的字段不推送到目标。无字典（未保存过）时行为与现状一致（全字段参与）。

#### Scenario: 源字段禁读后不产生转换结果

- **WHEN** 源连接字段字典标记某映射字段 `readable=false`
- **THEN** 该字段在 transform 输出中被剔除
- **AND** 删除同步的 records_full/源键集基于过滤后的记录计算

#### Scenario: 目标字段禁写后不出现在推送载荷

- **WHEN** 目标连接字段字典标记某映射字段 `writable=false`
- **THEN** 推送载荷（REST body / DB upsert dict / HAP controls）中不包含该字段
- **AND** 删除同步键集不受目标侧过滤影响

#### Scenario: 无字段字典时行为不回归

- **WHEN** 源/目标连接均未保存过字段字典
- **THEN** 同步按映射全量字段读取/推送，与现状完全一致