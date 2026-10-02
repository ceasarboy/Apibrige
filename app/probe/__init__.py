"""探针内核 — 三级验证、Schema 推断、映射草案校验、会话状态机

模型无关：L1/L2 全程不依赖任何 LLM；无模型时映射草案退化为同名匹配。
与运行时同源：传输一律经 ProtocolAdapterFactory / DatabaseConnector。
"""
