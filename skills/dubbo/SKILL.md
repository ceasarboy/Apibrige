# Dubbo 协议探针说明

## 连接配置（探针 / 建连接通用）

```json
{
  "protocol": "dubbo",
  "base_url": "zookeeper://host:2181 或直连 host:port",
  "timeout_ms": 30000
}
```

## 协议特有参数

约定放在 `mapping_config["protocol"]` 子字典（探针与运行时读同一对象）：

```json
{
  "interface": "接口全名（必填）",
  "method": "方法名（必填）",
  "registry": "zookeeper|direct"
}
```

## 探针示例

```bash
python app/probe/cli.py probe   --source-url "http://源系统:端口" --source-endpoint "方法名"   --target-url "https://目标系统" --target-endpoint "/api/target"   --protocol dubbo
```

## 注意事项

- 泛化调用，无需依赖 jar
- interface/method 缺失时返回指明键名的错误
