# add-bi-detail-query 提案

## Why

大表演进（`docs/bi-evolution-plan.md`）第一步。并发实测暴露的真瓶颈在明细链路：

- 服务端 `GET /bi/form-rows` 一次性回传全量行（实测 9.5 万行 0.14s 回传，但网络与内存随行数线性放大）；
- 前端明细卡（去分页改滚动后）**全量渲染 DOM**——万行级即卡死浏览器；
- 用户明确要求"明细大表逐行分析"（翻页、过滤、排序）。

## What Changes

1. **服务端分页/过滤/排序**：`GET /bi/form-rows` 扩展 `page/page_size/sort/order/keyword`
   （缺省保持旧行为向后兼容）。
   - DB 源：**下推 SQL**（`WHERE 关键字 LIKE` / `ORDER BY` / `LIMIT OFFSET` + COUNT），
     分页不随总行数退化；
   - HAP 源：走 `getFilterRows` 原生分页（pageSize≤1000 已有基建）；
   - 返回体加 `total`（count）。
2. **明细卡前端虚拟滚动**：`DetailTable` 改可视区渲染（复用 antd 内置的 `rc-virtual-list`，
   零新依赖），服务端翻页/排序/过滤；投放态自动轮播保留（虚拟滚动下按数据行驱动）。
3. **上限分档**：明细/聚合的单源上限语义从"10 万行硬闸"调整为"分档提示"
   （≤10 万正常；10~50 万警告；>50 万建议 DB 源下推/增量——下推在 P3）。
4. 验收基准：20 万行表翻页单页响应 <300ms、浏览器内存稳定、投放轮播不受影响。

## Impact

- **specs**：`bi-aggregation` 修改「表单明细查询」（分页/过滤/排序与下推）
- **代码**：`app/api/bi.py`（form-rows 参数与下推）、`app/services/bi_sources.py`
  （DB 分页查询函数；HAP 分页）、`frontend/src/pages/bi/BiBoard.tsx`（DetailTable 虚拟滚动）
- **兼容**：无参数调用（旧前端/scripts）行为不变；HAP 行键口径不变
- **不做**（后续期）：聚合下推（P3）、增量拉取（P4）、分析工作台（P2）
