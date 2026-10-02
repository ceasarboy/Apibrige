# WebSocket 协议探针说明

## 连接配置（探针 / 建连接通用）

```json
{
  "protocol": "websocket",
  "base_url": "ws://host:port/path",
  "timeout_ms": 30000
}
```

## 协议特有参数

约定放在 `mapping_config["protocol"]` 子字典（探针与运行时读同一对象）：

```json
{
  "subscribe_message": "连接后发送的订阅消息（可选）"
}
```

## 探针示例

```bash
python app/probe/cli.py probe   --source-url "http://源系统:端口" --source-endpoint "路径"   --target-url "https://目标系统" --target-endpoint "/api/target"   --protocol websocket
```

## 注意事项

- 采用拉取后断开模式，不支持持久订阅式源（既有约束）
- 每次探针/同步都会新建连接
