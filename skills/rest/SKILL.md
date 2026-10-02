---
name: apibridge-rest
description: 配置 APIBridge REST 同步的认证、请求包装及业务响应契约；配合探针 CLI 复验现有映射。
---

# REST 协议探针说明

## 连接配置（探针 / 建连接通用）

```json
{
  "protocol": "rest",
  "base_url": "https://host:port",
  "auth_type": "bearer|basic|api_key|custom",
  "auth_config": {
    "token": "..."
  },
  "headers": {
    "X-Custom": "v"
  },
  "timeout_ms": 30000
}
```

## 协议特有参数

无认证使用 `auth_type: null`；Content-Type 属于普通 `headers`，不必选择认证。敏感 Header 值在管理界面保存，文档和命令参数中使用占位值。

新映射使用 `mapping_config.version=2`，REST 目标配置放 `protocol.target`，HAP 源配置放 `protocol.source`：

```json
{
  "version": 2,
  "fields": [{"source":"code","target":"materialCode","transform":"string"}],
  "mapping_options": {"missing_source":"error","conversion_error":"error"},
  "protocol": {
    "target": {
      "method":"POST",
      "request":{"mode":"wrapped_array","records_path":"/data","max_records":1000,"on_empty":"skip"},
      "response":{"success_mode":"http_and_body","conditions":[{"path":"/success","operator":"eq","value":true}],"message_path":"/msg"}
    }
  }
}
```

`raw/array/object/wrapped_array` 决定 HTTP 请求体；记录字段不带 `data.` 前缀。路径使用 JSON Pointer，只支持对象字段嵌套。超限发送前失败，不自动拆批或重放。响应比较严格区分布尔值、数字和字符串。`http_only` 仅验证 HTTP，不代表业务处理成功。

已有映射复验用 `probe-mapping --mapping-id ...` 创建探针会话，再 `verify --level L1`、`infer`、`draft --mapping-config-file ...`、`verify --level L2`。配置文件仅含映射规则；凭据和工作表 ID 不写入可提交的文件。L2 不写目标，无契约不算通过；L3 继续要求管理界面人工授权。

旧版兼容参数如下（未配置 version 时保持原行为）：

约定放在 `mapping_config["protocol"]` 子字典（探针与运行时读同一对象）：

```json
{
  "method": "POST（push 默认方法，可省略）"
}
```

## 探针示例

```bash
python app/probe/cli.py probe   --source-url "http://源系统:端口" --source-endpoint "/path（fetch 为 GET，push 为 POST）"   --target-url "https://目标系统" --target-endpoint "/api/target"   --protocol rest
```

## 注意事项

- 端点支持 `{{变量}}`（如 `{{timestamp}}`），探测与运行时同一解析引擎
- `auth_type` 可选 bearer/basic/api_key；auth_config 按类型给 token 或 username/password
- fetch_data 对端点发 GET；push_data 对端点发 JSON POST
