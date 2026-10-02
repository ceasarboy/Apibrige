# add-bi-detail-query 任务清单（已归档）

## 1. 服务端分页（拍板后执行）

- [x] `bi_sources.fetch_form_rows_page`：DB 源下推（标识符白名单 + 参数绑定 +
      ORDER BY + LIMIT/OFFSET + COUNT；>100 万行近似 total 标记）
- [x] HAP 源分页分支（getFilterRows 原生分页 + count）
- [x] `/bi/form-rows` 扩展 page/page_size/keyword/sort_field/order 参数
      （缺省保持旧行为向后兼容）
- [x] `FORM_ROWS_WARN=50000` 分档提示（日志 + 响应标记）
- [x] 单测：SQL 生成/翻页正确性（SQLite 1 万行对照）/HAP 分支/旧调用兼容

## 2. 前端虚拟滚动

- [x] DetailTable 改 rc-virtual-list 渲染（行高 34px，可视区渲染）
- [x] 服务端分页器 + 表头排序 + 关键字过滤（300ms 防抖）
- [x] 投放态自动轮播在虚拟列表上的行为回归（rAF 逻辑不变）
- [x] tsc + Playwright 实测（20 万行：DOM 行数受控、滚动流畅、无控制台错误）

## 3. 验收与收口

- [x] 20 万行实测（perf_big 扩样）：翻页单页 <300ms
- [x] 三个既有 UI 自审脚本零回归 + 全量 pytest
- [x] 文档（bi-dashboard-guide / skill 明细章节）+ OpenSpec 归档
