# add-bi-hap-incremental 技术设计

## 1. 增量读行（适配器契约）

```python
# app/adapters/hap.py
async def get_worksheet_rows(
    worksheet_id, view_id=None, page_size=1000, max_pages=100,
    filters: list[dict] | None = None,          # V2 filters（**不是 filterControls**）
    sort: tuple[str, str] | None = None,        # (sortId, "asc"|"desc")
    return_total: bool = False,                 # 需要 total 时返回 (rows, total)
) -> list[dict] | tuple[list[dict], int | None]

async def get_rows_changed_since(
    worksheet_id, since: str | None, *, page_size=1000, max_pages=10,
    overlap_seconds: int = 1, view_id=None,
) -> tuple[list[dict], int | None]:
    """按 utime 取"最近变化"的行（新增 + 修改），返回 (rows, total)

    filters=[{"controlId": "utime", "dataType": 16, "filterType": 13,
              "value": since - overlap_seconds}]   # gt = 严格大于（实测）
    sortId="utime", sortType="desc"                # 最新在前（实测跨页稳定）
    since 为 None → 只取第一页（建立基线，不全量扫）
    """
```

**实测依据**（`scripts/hap_incremental_probe.py`，真实端点）：
`gt`(filterType 13) 对日期时间字段（dataType 16）与数值字段（dataType 6）都是**严格大于**，
`sortId=utime` 跨页单调不增且 rowid 无重复；`filterControls` 参数名**无效**（静默忽略）。

时间格式与行内 `utime` 一致（`"YYYY-MM-DD HH:mm:ss"`），比较按服务端口径。

## 2. 快照存储与拉取策略（`app/services/bi_sources.py`）

```python
_hap_snapshots: dict[str, dict] = {}
# key = f"{connection_id}:{form_key}"
# value = {
#   "rows": dict[str, dict],        # rowid → 行（幂等 upsert；顺序 = 首次出现顺序）
#   "watermark": str | None,        # 已见最大 utime
#   "total": int | None,            # 服务端 total（规模判定与占比统计）
#   "full_at": float,               # 上次全量对账时刻（monotonic）
#   "last_pull": float,             # 上次增量拉取时刻
#   "mode": "full" | "incremental",
#   "delta_rows": int,              # 最近一轮实际回传行数
# }
```

`fetch_form_rows(session, connection_id, form_key)`（HAP 分支）：

```
snapshot 不存在 or now-full_at > RECONCILE(600s)
    or 无 watermark or 上次缺 rowid/utime  → 全量拉（现状路径）→ 重建 rows 索引 + 水位线
否则:
    delta, total = get_rows_changed_since(ws, watermark, overlap=1s)
    if total 超过阈值（> max(快照行数/2, DELTA_ABS_CAP=5000)）→ 全量拉（大范围变更）
    else → rows.update({r["rowid"]: r for r in delta})（幂等 upsert）
    watermark = max(watermark, max(utime in delta))
    last_pull = now
```

- **同秒漏采的规避**：`watermark − 1s` 重叠窗口 + rowid 幂等 upsert——窗口内「与水位线同秒」的行会被重复回传（有界、幂等吸收，行数在 `boundary_rows` 可见；水位线前进后即缩小为最新一批）；
  重叠行重复回传是**无害**的（同一行最新态覆盖），代价是多拉极少量行。
- **删除的让渡**：HAP 无删除事件接口 → 删除行在下一次全量对账（默认 ≤600s）后消失；
  这是增量方案换来的速度的显式代价，写进响应与文档。
- **上限不变**：合并后的行列表仍按 `MAX_FORM_ROWS=10 万` 截断并告警（P3 规格）。
- **并发**：沿用既有 `_form_locks` per-key 锁；快照读写都在锁内。

配置（模块常量 + 环境变量覆写）：

| 常量 | 默认 | 作用 |
|---|---|---|
| `HAP_INCREMENTAL_MIN_ROWS` | 20000 | 工作表 `total` 超过它才启用增量（小表保持全量，删除/变更 60s 内可见） |
| `HAP_INCREMENTAL_RECONCILE_SECONDS` | 600 | 全量对账间隔 |
| `HAP_INCREMENTAL_OVERLAP_SECONDS` | 1 | 水位线回退窗口（秒粒度下 1s 即覆盖「同水位线秒」的行） |
| `HAP_INCREMENTAL_DELTA_ABS_CAP` | 5000 | 单轮增量行数上限（超过改全量） |
| `APIBRIDGE_HAP_INCREMENTAL=0` | — | 一键关闭（排查/对照用） |

## 3. 可观测性

`GET /bi/form-rows` 响应追加：

```json
"snapshot": {"mode": "incremental", "delta_rows": 2, "total_rows": 144,
             "watermark": "2026-09-29 10:11:12", "full_at_ago_s": 37}
```

聚合路径（`_scan_form`）日志一行：`HAP 增量: form=… mode=incremental delta=2 total=144`。
验收口径"增量轮次拉取行数占比"= `delta_rows / total_rows`，直接可读。

## 4. 测试

单测（`tests/test_bi_hap_incremental.py`，用假适配器，不打网络）：

| 用例 | 断言 |
|---|---|
| 首轮 | mode=full，快照建立，水位线 = max utime |
| 增量新增 | 只回传新行；合并后与全量一致；水位线前进 |
| 增量修改 | 改一行后 utime 变新 → 该行回传并覆盖旧值 |
| 同秒重叠 | 重叠窗口内已见行被重复回传 → 幂等（值不变、行数不变） |
| 超阈值 | `total` 超 DELTA_ABS_CAP → 回退全量（可观测 mode=full） |
| 小表 | `total < MIN_ROWS` → 始终全量（mode=full，无增量请求） |
| 缺字段 | 行无 rowid/utime → 回退全量 |
| 删除 | 增量轮次不感知；对账轮（把 full_at 拨老）后消失 |
| 上限 | 合并后 > MAX_FORM_ROWS → 截断 + warn |
| 开关 | `APIBRIDGE_HAP_INCREMENTAL=0` → 全量 |

真实端点验证（脚本 `scripts/hap_incremental_verify.py`）：
把阈值调 0 → 首轮全量 → 用 API 新增 2 行、改 1 行 → 增量轮次**只回传 3 行**
（占 144 行 2.1% < 5%）→ 合并快照与全量拉取逐行一致 → 删除 1 行 → 对账轮后消失。
脚本自清理（删除自己新增的行）。

## 5. 风险与处置

| 风险 | 处置 |
|---|---|
| utime 秒级粒度导致同秒变更漏采 | 1s 重叠窗口（覆盖同秒）+ rowid 幂等 upsert（重复无害）；同秒行数极大时由 `delta_too_large` 回退全量兜底 |
| 服务端按 utime 过滤无索引 → 慢 | 一次请求拿 `total` 与首页即可判规模；超阈值直接改全量，不硬撑 |
| 内存快照与服务端不一致（删除） | 定期全量对账（默认 600s）+ 响应暴露 `full_at_ago_s` |
| 多 worker 部署各自快照 | 行为正确（各自全量+增量），只是无共享；本项目单 worker（AGENTS §6） |
| 语义变化影响既有看板 | 仅对 `total > 20000` 的表启用；小表路径字节级不变（既有测试覆盖） |

## 6. 落地记录与偏差（2026-09-29 实施）

提交：`feat(P4)`（适配器 + 策略 + 单测 + 真实验证脚本）、`docs` 收口。

### 6.1 两处实测驱动的偏差（原设计 → 实际）

**偏差一：重叠窗口 → 两段式（严格大于 + 边界刷新）**
原设计用"水位线 − 2s 重叠窗口"防同秒漏采。实测（真机）暴露它的代价：
**与水位线同秒的行会被整批重拉**——本表 144 行全落在同一秒（批量导入），
于是"增量"= 整表，功能形同虚设（且会触发 `delta_too_large` 回退全量）。
改为两段式：
1. `utime > 水位线`（严格）→ **真增量**（新增 + 变更）；
2. `utime >= 水位线` → 与真增量做**客户端差集** → 得到"恰好同秒"的边界集，
   行数超 `HAP_INCREMENTAL_BOUNDARY_MAX_ROWS`（默认 2000）即跳过并标
   `boundary_skipped`（宁可漏采同秒变更，也不让边界集=整表的表把增量拖成全量；
   兜底是对账）。

**偏差二：服务端同字段多条件是 OR，不是 AND**
想要 `=W` 精确过滤需要 `utime >= W AND utime <= W`，实测两个同 controlId 的过滤条件
被 OR 组合（返回 144 = 整表），而 `filterType=2/17` 的等值语义在日期时间字段上又不命中
（返回 0）。故边界集只能靠客户端差集拿（第 1 步已把真增量取回，多一趟 `ge` 请求即可）。

### 6.2 真实验证（scripts/hap_incremental_verify.py，api.mingdao.com，全过）

| 步骤 | 结果 |
|---|---|
| 首轮 bootstrap | 快照 144 行 = 源端 144 行 |
| 造变化（新增 2 / 修改 1） | 新增返回 rowid ✓ |
| 首轮增量 | `mode=incremental`，真增量 **2**，边界刷新 **139**（基线秒整批，**预期代价**） |
| 稳态增量（水位线已前进） | 真增量 0 + 边界 3 → **2.04% < 5%**；同秒新行由边界刷新兜住（快照 147） |
| 合并快照 vs 直接全量 | 147 vs 147，**逐行字段零差异** |
| 删除 | 增量轮次仍持有已删行（快照 147）——让渡语义如实呈现，对账轮清除 |
| 清理 | 源端无残留自建行 |

**同秒新行被边界刷新救回**是本轮最有价值的实测：脚本在 `[3]` 轮拉取后立刻新增一行，
其 `utime` 与水位线**同秒** → 严格大于查询返回 0，边界刷新把它捞回来了（快照因此从
146 变 147 且与源端一致）——这正是两段式设计要防的漏采。

### 6.3 已知边界（写进文档与响应字段）

- **删除滞留**：增量轮次不感知删除，最迟在对账间隔（默认 600s）后消失；
  响应 `snapshot.full_at_ago_s` 让外界可判断"当前快照有多旧"。
- **首轮增量的边界代价**：若基线表的数据集中在同一秒（批量导入），首轮增量会刷新整表
  （有界，受上限约束）；水位线前进后即收敛。
- **验证脚本要求后端快照干净**（进程内状态）：脚本 `[0]` 会校验
  `mode=full/reason=bootstrap/行数一致`，不满足即提示重启后端——上一轮验证据留下的
  已删行会滞留到对账，属设计使然。
- HAP 源仍受 `MAX_FORM_ROWS=10 万` 硬闸（P3 规格不变）；默认阈值 20000 行以下走全量
  （小表删除/变更 60s 内可见的现状语义不变）。
