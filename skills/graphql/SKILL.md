# GraphQL 协议探针说明

## 连接配置（探针 / 建连接通用）

```json
{
  "protocol": "graphql",
  "base_url": "https://host:port/graphql",
  "auth_type": "bearer",
  "auth_config": {
    "token": "..."
  },
  "timeout_ms": 30000
}
```

## 协议特有参数

约定放在 `mapping_config["protocol"]` 子字典（探针与运行时读同一对象）：

```json
{
  "query": "GraphQL 查询语句"
}
```

## 探针示例

```bash
python app/probe/cli.py probe   --source-url "http://源系统:端口" --source-endpoint "操作名或完整 query"   --target-url "https://目标系统" --target-endpoint "/api/target"   --protocol graphql
```

## 注意事项

- base_url 通常以 /graphql 结尾
- L2 拉取样本要求响应为 JSON（data 键会被自动展开）
