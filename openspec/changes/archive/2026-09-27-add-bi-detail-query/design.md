# add-bi-detail-query 技术设计

## 1. 服务端分页查询（app/services/bi_sources.py）

```python
async def fetch_form_rows_page(session, connection_id, form_key,
                               page=1, page_size=100, keyword=None,
                               sort_field=None, order="asc") -> (rows, total)
```

- **DB 源**（下推，走连接器）：
  - 标识符校验（表名/列名白名单化——复用 `EngineProvider.validateIdentifier` 同款思路：
    仅 `[A-Za-z0-9_\u4e00-\u9fa5]`，长度 ≤64，杜绝注入）；
  - `SELECT * FROM t [WHERE CAST(关键列 AS TEXT) LIKE ?] ORDER BY col LIMIT ? OFFSET ?`
    （keyword 命中列由调用方给出或对全部文本列 OR——首版：**表头任一文本列 OR LIKE**，
    列集合来自表结构，参数化绑定）；
  - `SELECT COUNT(*)` 单独执行（大表可加近似策略：>100 万行时 COUNT 用
    `MAX(rowid)`/`REPLACE` 估算标记 `total_approx=true`）。
- **HAP 源**：`getFilterRows` 原生分页（pageNo/pageSize≤1000）——页大小对齐 1000 内；
  keyword 走 API 的 filter 或本地过滤（首版本地过滤当前页 + 提示）；total 用返回 count。
- 旧调用（无 page 参数）：保持"全量拉取截断 limit"旧行为（scripts/旧前端不破坏）。

## 2. 明细卡虚拟滚动（frontend DetailTable）

- 引入 `rc-virtual-list`（antd 的既有传递依赖，`import VirtualList from 'rc-virtual-list'`，
  零新增包）；
- 数据流：`/bi/form-rows?page_size=200&page=n` → 虚拟列表行高 34px；滚动到底自动加载下一页
  （无限滚动）或显式分页器（首版：**服务端分页器 + 虚拟列表渲染当前页**，简单可控）；
- 排序/过滤：表头点击排序（sort_field+order）、关键字输入框（300ms 防抖）；
- 投放态自动轮播：保留 rAF 恒速滚动逻辑（作用于虚拟列表容器）；数据量小（单页 200 行）时
  轮播行为与现状一致。

## 3. 上限分档

- `MAX_FORM_ROWS=100000` 保留为"硬护栏"，新增 `FORM_ROWS_WARN=50000`：
  拉取行数超警告线时聚合日志 warning + API `?warn` 提示；>硬上限维持 502 明确报错并
  提示"该表建议使用 DB 源下推（P3）或增量（P4）"。

## 4. 测试

- 单测：分页下推 SQL 生成（标识符白名单/参数绑定/排序方向）、DB 分页正确性（SQLite 造
  1 万行，翻页与 ORDER BY 对照）、HAP 分页分支（mock）、兼容旧调用；
- 端点测：`/bi/form-rows` 带/不带分页参数；
- 实测：20 万行表（perf_big 扩样）翻页 <300ms、UI 虚拟滚动流畅（Playwright 断言
  DOM 行数 < 可视区×2、内存稳定）、对既有自审脚本零回归。
