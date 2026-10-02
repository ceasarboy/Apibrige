---
name: apibridge-cli
description: APIBridge 探针 CLI 入口，用于确定性联调、复验现有映射及提交待确认清单；批准仅限管理界面。
---

# apibridge CLI 使用说明（agent 必读）

APIBridge 联调平台的 agent 唯一入口。本 CLI 只经 HTTP 与服务端通信，可在任何装有 Python 3 的机器上独立运行（无需安装项目依赖）。

## 0. 前置条件

```bash
export APIBRIDGE_SERVER="http://<服务地址>:8000"   # 必填
export APIBRIDGE_ADMIN_TOKEN="<管理Token>"          # 必填，向平台管理员获取
```

- **不接受 `--token` 命令行参数**，token 只能来自环境变量
- 缺环境变量时退出码 4，错误信息指明缺哪个

调用方式（无需安装）：

```bash
python /path/to/app/probe/cli.py <子命令> ...
```

## 1. 输出契约（--json 语义，schema_version=1.0）

每个命令输出一个 JSON 对象：

```json
{"schema_version": "1.0", "ok": true, "data": {...}, "error": null}
```

失败时 `ok=false`、`data=null`、`error` 含 `code`/`message`/`next_action`（下一步怎么做）。**字段名一经发布不会更改。**

## 2. 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 验证未通过（L2 比对不通过等） |
| 3 | 被拒绝（鉴权失败 / 预算耗尽 / 状态不足 / 未授权 L3） |
| 4 | 调用错误（缺环境变量 / 参数错误 / 服务不可达） |

## 3. 典型联调剧本（从裸 URL 到待确认清单）

```bash
# 1) 建会话并做 L1 探测（可达性+认证；不创建连接、不搬业务数据）
python app/probe/cli.py probe \
  --source-url "http://源系统:端口"  --source-endpoint "/api/users" \
  --target-url "http://目标系统:端口" --target-endpoint "/api/customers"
# → data.status == "L1_reachable" 才继续；否则看 error/next_action

# 2) 推断源字段清单（path/type/nullable/is_array/sample_value）
python app/probe/cli.py infer --session <SESSION_ID>

# 3) 提交字段对应关系（缺省 --fields 生成"同名+类型兼容"退化草案）
python app/probe/cli.py draft --session <SESSION_ID> \
  --fields '[{"source":"user_name","target":"customer_name","transform":"string"}]'
# 退出码 2 = 校验未通过，看 data.issues 逐项修正

# 4) L2 验证：真实拉取 ≤20 条样本 + 真实转换 + 目标契约比对（不写目标）
python app/probe/cli.py verify --session <SESSION_ID> --level L2

# 5) 提交至待确认清单（不会建桥；人在管理界面批准）
python app/probe/cli.py protocol apply --session <SESSION_ID>

# 随时查看
python app/probe/cli.py session show --session <SESSION_ID>
python app/probe/cli.py review list
python app/probe/cli.py review show --session <SESSION_ID>
```

## 3.5 常见误解澄清

已有映射复验：`probe-mapping --mapping-id <ID>` 只创建探针会话，沿用现有连接和完整映射配置，不创建桥接对象。可以用 `--mapping-config-file <JSON文件>` 指定待验证规则，`--target-endpoint` 指定草稿目标端点。随后执行 `verify --level L1`、`infer`、`draft --mapping-config-file <JSON文件>`、`verify --level L2`。配置文件可包含 v2 源/目标协议参数、validations 和 target_contract；凭据仍只在环境变量或已保存连接中，实际工作表 ID 不写入可提交文件。

- **APIBRIDGE_ADMIN_TOKEN 不是大模型的 API Key**。它是 APIBridge 平台自身管理接口的
  访问口令（在服务端 .env 中配置）。
- **大模型（地址/模型名/Key）配置在你本机的 agent 工具里**（Trae / Claude Code 等），
  APIBridge 服务端零模型依赖——模型不碰平台，平台只做确定性校验与人工把关。

## 4. 硬性边界（勿尝试绕过）

- **CLI 没有批准能力**：`review approve` 直接拒绝（退出码 3）。建桥的唯一通路是人在管理界面「待确认清单」点批准。
- **L3（写 1 条真实数据到目标）需人工授权**：未授权时返回 `requires_human_authorization`。请引导工程师在清单页点「授权 L3 验证」后重试 `verify --level L3`。
- 单会话对同一目标默认 **≤10 次**探测调用，超出返回 `probe_budget_exhausted`——应新建会话或停止重试，不要循环打目标。
- 目标地址受白名单约束：回环/RFC1918/链路本地/云元数据地址默认拒绝（`target_not_allowed`），内网目标需管理员配置 `APIBRIDGE_PROBE_ALLOWED_HOSTS`。

## 5. 协议说明

各协议（rest/graphql/soap/grpc/websocket/dubbo/hap）的连接配置与特有参数见 `skills/<协议名>/SKILL.md`。协议特有参数统一放 `mapping_config["protocol"]` 子字典。
