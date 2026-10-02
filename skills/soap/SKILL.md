# SOAP 协议探针说明

## 连接配置（探针 / 建连接通用）

```json
{
  "protocol": "soap",
  "base_url": "https://host/service?wsdl",
  "auth_type": "basic",
  "auth_config": {
    "username": "u",
    "password": "p"
  },
  "timeout_ms": 60000
}
```

## 协议特有参数

约定放在 `mapping_config["protocol"]` 子字典（探针与运行时读同一对象）：

```json
{
  "operation": "WSDL 中的操作名"
}
```

## 探针示例

```bash
python app/probe/cli.py probe   --source-url "http://源系统:端口" --source-endpoint "操作名"   --target-url "https://目标系统" --target-endpoint "/api/target"   --protocol soap
```

## 注意事项

- 首次调用会拉取并解析 WSDL，超时建议 ≥30s
- 请求参数经 mapping_config["fields"] 组装
