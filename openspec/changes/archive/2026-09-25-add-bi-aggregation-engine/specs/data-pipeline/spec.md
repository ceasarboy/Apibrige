## ADDED Requirements

### Requirement: 中间数据明细保留与清理

系统 SHALL 为 `data_buffer` 明细数据提供保留策略与清理能力，避免无限增长；聚合结果 SHALL 不受明细清理影响。

#### Scenario: 超过保留期的明细被清理

- **WHEN** 存在创建时间早于保留期的 `data_buffer` 明细
- **THEN** 清理任务 MUST 分批删除这些明细（单批行数有上限）
- **AND** MUST NOT 在单个事务中删除全部（避免长事务与锁竞争）

#### Scenario: 保留期可配置

- **WHEN** 保留天数配置项被调整
- **THEN** 清理行为 MUST 按新配置生效
- **AND** MUST NOT 需要改动代码

#### Scenario: 清理不影响聚合结果

- **WHEN** 明细数据被清理后
- **THEN** `bi_metric_buckets` 中对应区间的聚合结果 MUST 保持可查询

#### Scenario: 清理失败不影响主链路

- **WHEN** 清理任务执行失败
- **THEN** MUST NOT 影响同步传输与聚合任务
- **AND** MUST 记录可诊断的失败日志
