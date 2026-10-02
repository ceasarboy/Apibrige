# add-bi-hap-incremental 提案（P4：HAP 源增量拉取）

## Why

HAP（明道云）源的聚合与明细**每次都全量拉工作表**：`get_worksheet_rows` 按 1000 行/页
自动翻页，10 万行 = 100 次往返，而 BI 快照聚合**每分钟一轮**。现状下大表 HAP 看板
每分钟把整表重拉一遍——这是 HAP 侧唯一的规模瓶颈（DB 侧已由 P3 下推解决）。

**实测证据（本轮 `scripts/hap_incremental_probe.py`，真实 api.mingdao.com）**：

| 探针 | 结果 | 结论 |
|---|---|---|
| 行内系统字段（`getSystemControl=True`） | 含 `rowid` / `ctime` / `utime` / `autoid` / `caid` / `uaid` | 有可用水位线与行标识 |
| `sortId=utime&sortType=desc` 跨 3 页 | `utime` 单调不增、rowid 无重复 | 服务端排序可用 |
| `filters=[{utime, dt16, ft13(gt), 2099}]` | 0 行（`total`=0） | **服务端过滤生效** |
| `filters=[{utime, dt16, ft13(gt), 2000}]` | 144 行 | gt 语义正确 |
| `filters=[{utime, dt16, ft13(gt), 边界秒}]` | 0 行 | **严格大于**，可直接做水位线 |
| `filters=[{autoid, dt6, ft13(gt), 999999}]` | 0 行 | 数值字段同样生效 |
| **`filterControls=[同上]`** | 144 行（等于无过滤） | **参数名是 `filters`；`filterControls` 被静默忽略**（易踩） |

即：**`utime > 水位线` 的服务端过滤是现成的**，无需翻页截止、无需客户端差集。
`utime` 同时覆盖"新增"与"变更"（更新行会刷新 utime），删除则必须靠定期全量对账。

## What Changes

1. **适配器能力**（`app/adapters/hap.py`）：`get_worksheet_rows` 增加可选
   `filters` / `sort` / `return_total` 参数（复用同一翻页循环），并新增
   `get_rows_changed_since(worksheet_id, since, *, overlap_seconds)`：
   `utime > (since − overlap)` + `utime desc` 翻页，返回 `(rows, total)`。
2. **增量快照存储**（`app/services/bi_sources.py`）：按 (连接, 表单) 维护进程内快照
   ——`{rowid: row}` 索引 + `watermark`（已见最大 `utime`）+ `full_at`（上次全量时间）+
   计数；`fetch_form_rows` 对 HAP 源走新路径：
   - **首次/超过对账间隔（默认 600s）/上次无水位线/缺 utime·rowid** → 全量拉（现状行为）；
   - 其余 → 增量拉（`utime > watermark − 2s` 重叠窗口，**按 rowid 幂等 upsert**，
     规避 utime 秒级粒度造成的同秒漏采）→ 更新水位线；
   - 若 `total` 超过"增量阈值"（快照行数的一半或绝对上限）→ 放弃增量改全量
     （大范围变更时全量比翻页便宜）。
3. **规模闸与开关**：仅在**工作表行数超过 `HAP_INCREMENTAL_MIN_ROWS`（默认 20000）**
   时启用增量（小表全量本来就快，且保持删除/变更 60s 内可见的现状语义）；
   可用环境变量 `APIBRIDGE_HAP_INCREMENTAL_MIN_ROWS` 覆写（含设 0 = 全量走增量，
   供小表验证与运维排查）、`APIBRIDGE_HAP_INCREMENTAL=0` 一键关闭。
4. **可观测性**：`GET /bi/form-rows`（含明细卡路径）响应附
   `snapshot: {mode: "full"|"incremental", delta_rows, total_rows, watermark, full_at}`，
   聚合路径记日志——增量轮次拉取行数占比可直接观测（P4 验收口径）。
5. **不变式（明确保留）**：HAP 源**仍受 `MAX_FORM_ROWS=10 万` 硬闸**（P3 规格已定；
   合并后的快照同样截断并告警）；删除的行在下一次全量对账（≤600s）后才从快照消失
   ——这是增量方案对"实时性"的显式让渡，写进文档与响应字段。

## Impact

- **specs**：`bi-aggregation` 修改「表单数据源」相关需求（HAP 拉行新增增量模式与对账语义）
- **代码**：`app/adapters/hap.py`（filters/sort/total + 增量读行）、
  `app/services/bi_sources.py`（快照存储 + 拉取策略）、`app/api/bi.py`（form-rows 附快照信息）
- **不做（非目标）**：
  - **同步引擎的 HAP 增量**（SyncTask 拉源）——语义不同（影响"推哪些行"、要与
    upsert/删差上下位协同），本期只做 BI 取数侧；探针结论可直接复用，另立变更
  - 删除的实时感知（HAP 无 tombstone 接口；靠定期全量对账）
  - 超过 10 万行的 HAP 工作表（上限语义不变）
  - 跨进程共享快照（本项目单 worker 运行；多 worker 时各自全量，行为正确但无加速）
- **验收**：
  1. 真实端点：在既有工作表上调低阈值 → 首轮全量 → 用 API 新增/修改行 → 增量轮次
     **只回传变化行**（拉取行数 / 总行数 < 5%），且**合并后快照与全量拉取逐行一致**；
  2. 删除行在下次全量对账后消失（对账语义可复现）；
  3. 单测：增量合并正确性（增/改/删+对账）、同秒重叠幂等、超阈值回退全量、
     小表保持全量、缺 utime/rowid 回退、上限截断；
  4. 全量 pytest 零回归 + 5 个 UI 自审脚本零回归（HAP 看板数值与前一致）。
