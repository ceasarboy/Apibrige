# gRPC 协议探针说明

## 连接配置（探针 / 建连接通用）

```json
{
  "protocol": "grpc",
  "base_url": "host:port（不带 http://）",
  "timeout_ms": 30000
}
```

## 协议特有参数

约定放在 `mapping_config["protocol"]` 子字典（探针与运行时读同一对象）：

```json
{
  "service_name": "服务全名（如 helloworld.Greeter，必填）",
  "proto_content": "proto 文件内容（必填，服务端动态编译）",
  "use_tls": false,
  "use_reflection": false
}
```

## 探针示例

```bash
python app/probe/cli.py probe   --source-url "http://源系统:端口" --source-endpoint "方法名（如 SayHello）"   --target-url "https://目标系统" --target-endpoint "/api/target"   --protocol grpc
```

## 注意事项

- **缺 service_name 或 proto_content 会在调用前报明确错误**（指明键名）
- 错误形如「gRPC 缺少必需参数 service_name（应配置在 mapping_config['protocol'].service_name…）」
