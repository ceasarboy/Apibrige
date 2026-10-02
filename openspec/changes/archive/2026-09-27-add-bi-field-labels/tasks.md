# add-bi-field-labels 任务清单

- [x] DetailTable：列显示名优先级映射（卡级别名 > 字段字典 name > 原始字段名），
      表头与排序下拉同步
- [x] 定义页「列显示名」编辑区（明细图表 + form 源：字段列表逐行输入别名，
      写入 style.column_labels；留空回落）
- [x] 校验：无新增（style 自由 JSON 透传）
- [x] 验收：perf_big 明细卡把 work_date 设"生产时间"——表头/排序下拉/放大弹层三处生效；
      Playwright 断言 + 控制台门禁
- [x] 文档（guide/skill 说明卡级别名与字段字典的优先级）+ OpenSpec 归档
