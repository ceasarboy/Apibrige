"""gRPC 协议适配器 — 基于 grpcio + 运行时 proto 编译，编译结果缓存"""
import asyncio
import hashlib
import importlib
import os
import sys
import tempfile
import time
from typing import Any
from app.adapters.base import ProtocolAdapter
from app.adapters.auth import AuthInjector


# 模块级缓存：proto 文本哈希 -> 编译后的 pb2 模块
_PROTO_CACHE: dict[str, tuple] = {}


class GrpcAdapter(ProtocolAdapter):
    """gRPC 协议适配器

    使用 grpcio + grpcio-tools 在运行时编译 proto 文本为 Python 模块，
    编译结果按 proto 内容哈希缓存到内存，避免重复编译。

    配置约定:
        config["base_url"]: gRPC 服务地址（host:port，如 localhost:50051）
        config["proto_content"]: .proto 文件内容字符串
        config["service_name"]: 调用的服务全名（如 helloworld.Greeter）
        config["use_tls"]: 是否启用 TLS（默认 False）
        config["use_reflection"]: 是否启用 server reflection（默认 False，启用时可省略 proto_content）

    fetch_data(endpoint, params):
        endpoint: 方法名（如 SayHello）
        params: 请求参数 dict（按 proto message 字段名）
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.auth = AuthInjector(
            config.get("auth_type"),
            config.get("auth_config"),
        )
        self.target = config.get("base_url", "")
        self.proto_content = config.get("proto_content", "")
        self.service_name = config.get("service_name", "")
        self.use_tls = config.get("use_tls", False)
        self.use_reflection = config.get("use_reflection", False)
        self._stub_cls = None

    async def _compile_proto(self) -> tuple:
        """编译 proto 文本为 Python 模块（线程池中执行，结果缓存）

        Returns:
            (pb2_module, pb2_grpc_module)
        """
        if not self.proto_content:
            raise ValueError("gRPC 适配器需要 config['proto_content']")

        # 用 proto 内容哈希做缓存 key
        proto_hash = hashlib.sha256(self.proto_content.encode()).hexdigest()
        if proto_hash in _PROTO_CACHE:
            return _PROTO_CACHE[proto_hash]

        def _compile():
            from grpc_tools import protoc

            with tempfile.TemporaryDirectory() as workdir:
                # 文件名带哈希：protoc 产物的 import 与全局 descriptor pool 都按
                # 文件名注册——固定名 service.proto 会让同进程第二个不同 proto
                # 编译/加载直接撞池（复审实证：duplicate file name service.proto）
                base = f"svc_{proto_hash[:12]}"
                proto_file = os.path.join(workdir, f"{base}.proto")
                with open(proto_file, "w", encoding="utf-8") as f:
                    f.write(self.proto_content)

                # 编译为 pb2.py 和 pb2_grpc.py
                protoc.main([
                    "grpc_tools.protoc",
                    f"-I{workdir}",
                    f"--python_out={workdir}",
                    f"--grpc_python_out={workdir}",
                    proto_file,
                ])

                # 按唯一模块名加载（审查 P2）：protoc 产物固定叫 service_pb2，
                # 用固定名 importlib 会命中 sys.modules 里第一个连接的旧模块
                # （第二连接静默绑旧契约/报"Stub 不存在"），且 sys.path 持续膨胀
                import importlib.util
                # 产物名 = svc_<hash>_pb2(_grpc)，天然唯一且 pb2_grpc 顶部的
                # `import svc_<hash>_pb2` 已可解析（注册进 sys.modules）
                pb2_name = f"{base}_pb2"
                grpc_name = f"{base}_pb2_grpc"
                modules = {}
                try:
                    for mod_name in (pb2_name, grpc_name):
                        spec = importlib.util.spec_from_file_location(
                            mod_name, os.path.join(workdir, f"{mod_name}.py"))
                        mod = importlib.util.module_from_spec(spec)
                        sys.modules[mod_name] = mod
                        spec.loader.exec_module(mod)
                        modules[mod_name] = mod
                except TypeError as e:
                    # protobuf 默认描述池按 package+符号名全局注册：两个 proto
                    # 复用同一 package 且消息同名时进程内必然冲突（与加载方式
                    # 无关）——给出可行动提示而非裸 TypeError
                    raise ValueError(
                        f"gRPC proto 加载失败：{e}。"
                        "若同进程存在多个 proto，请为每个 proto 使用不同的 package 名"
                    ) from e
                return modules[pb2_name], modules[grpc_name]

        pb2, pb2_grpc = await asyncio.to_thread(_compile)
        _PROTO_CACHE[proto_hash] = (pb2, pb2_grpc)
        return pb2, pb2_grpc

    async def _get_stub(self):
        """创建并缓存 gRPC Stub 类"""
        if self._stub_cls is None:
            pb2, pb2_grpc = await self._compile_proto()
            # service_name 形如 "helloworld.Greeter"，对应 pb2_grpc.GreeterStub
            short_name = self.service_name.split(".")[-1] if self.service_name else ""
            if not short_name:
                raise ValueError(
                    "gRPC 缺少必需参数 service_name"
                    "（应配置在 mapping_config['protocol'].service_name 或连接配置中）"
                )
            stub_attr = f"{short_name}Stub"
            if not hasattr(pb2_grpc, stub_attr):
                raise ValueError(f"proto 中未找到 Stub: {stub_attr}")
            self._stub_cls = getattr(pb2_grpc, stub_attr)
            self._pb2 = pb2
        return self._stub_cls, self._pb2

    def _build_request(self, pb2, method_name: str, params: dict):
        """根据方法名查找 Request message 并填充参数"""
        # 约定：proto 中定义的 Request message 名为 {Method}{Request} 或 {Method}Request
        candidates = [
            f"{method_name}Request",
            f"{method_name.capitalize()}Request",
        ]
        for name in candidates:
            if hasattr(pb2, name):
                cls = getattr(pb2, name)
                return cls(**params)
        # 退化策略：用 Google Struct 包装
        raise ValueError(f"proto 中未找到方法 {method_name} 的 Request message")

    async def _call(self, method_name: str, params: dict) -> Any:
        """调用 gRPC 方法"""
        import grpc

        stub_cls, pb2 = await self._get_stub()

        # 构造 channel
        if self.use_tls:
            channel = grpc.aio.secure_channel(self.target, grpc.ssl_channel_credentials())
        else:
            channel = grpc.aio.insecure_channel(self.target)

        # 注入认证 metadata
        headers, _ = await self.auth.inject_async(self.headers)
        metadata = [(k, str(v)) for k, v in headers.items()]

        try:
            stub = stub_cls(channel)
            request = self._build_request(pb2, method_name, params)
            method = getattr(stub, method_name)
            response = await method(request, metadata=metadata, timeout=self.timeout)
            return self._message_to_dict(response)
        finally:
            await channel.close()

    @staticmethod
    def _message_to_dict(msg) -> dict:
        """将 protobuf message 转为 dict（保留字段名）"""
        from google.protobuf.json_format import MessageToDict
        return MessageToDict(msg, preserving_proto_field_name=True)

    def _require_service_name(self) -> None:
        """缺 service_name 时在编译 proto 前即报错，指明参数键名（task 1.10）"""
        if not self.service_name:
            raise ValueError(
                "gRPC 缺少必需参数 service_name"
                "（应配置在 mapping_config['protocol'].service_name 或连接配置中）"
            )

    async def fetch_data(self, endpoint: str, params: dict | None = None) -> Any:
        """调用 gRPC 方法拉取数据"""
        self._require_service_name()
        return await self._call(endpoint, params or {})

    async def push_data(self, endpoint: str, data: Any, method: str = "POST") -> dict:
        """调用 gRPC 方法推送数据"""
        self._require_service_name()
        params = data if isinstance(data, dict) else {"data": data}
        result = await self._call(endpoint, params)
        return {"status_code": 200, "body": result}

    async def test_connection(self) -> dict:
        """测试 gRPC 端点可达性"""
        import grpc

        start = time.time()
        try:
            if self.use_tls:
                channel = grpc.aio.secure_channel(self.target, grpc.ssl_channel_credentials())
            else:
                channel = grpc.aio.insecure_channel(self.target)
            try:
                # 等待 channel 就绪
                await asyncio.wait_for(
                    channel.channel_ready(),
                    timeout=min(self.timeout, 5.0),
                )
                latency = int((time.time() - start) * 1000)
                return {"status": "ok", "latency_ms": latency, "error": None}
            finally:
                await channel.close()
        except Exception as e:
            return {"status": "failed", "latency_ms": int((time.time() - start) * 1000), "error": str(e)}
