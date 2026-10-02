---
name: apibridge-hap
description: APIBridge 明道云工作表 V2 同步连接、鉴权及字段 ID/别名解析配置。
---

# HAP（明道云工作表 V2）协议探针说明

## 连接配置（探针 / 建连接通用）

```json
{
  "protocol": "hap",
  "base_url": "https://api.mingdao.com",
  "timeout_ms": 30000
}
```

## 协议特有参数

约定放在 `mapping_config["protocol"]` 子字典（探针与运行时读同一对象）：

```json
{
  "worksheet_id": "工作表 ID"
}
```

## 探针示例

```bash
python app/probe/cli.py probe   --source-url "http://源系统:端口" --source-endpoint "操作（getRows/createRow 等）"   --target-url "https://目标系统" --target-endpoint "/api/target"   --protocol hap
```

## 注意事项

- 连接的 `auth_config` 接受 HAP-Appkey/HAP-Sign 等兼容键，按敏感值保存。适配器使用 V2 JSON body/query 中的 appKey/sign，并非把这两个配置键直接作为工作表请求头。
- 新版 `mapping_config.version=2` 时源参数放 `protocol.source`，目标参数放 `protocol.target`。源端 `field_keys` 可设 raw/alias/id/alias_or_id；旧版默认 raw。
- 源响应可能使用 alias，字段目录的 path 是推荐读取键，write_path 才是写入 controlId。alias_or_id 根据真实元数据兼容两种键；冲突报错，不猜中文名称。L2 和运行时复用同一解析规则。
- 读取使用 getFilterRows（最多1000条/页）；写入 addRow 的 controls 为数组。L3 写真实数据必须先获人工授权。
- 详细接口约定见明道云开放平台文档
