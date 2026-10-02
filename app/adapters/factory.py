"""协议适配器工厂 — 按协议类型创建适配器实例"""
from app.adapters.base import ProtocolAdapter


class ProtocolAdapterFactory:
    """协议适配器工厂

    根据 protocol 类型从已注册的适配器中创建实例。
    新增协议只需在 _REGISTRY 中注册。
    """

    _REGISTRY: dict[str, type[ProtocolAdapter]] = {}

    @classmethod
    def register(cls, protocol: str, adapter_class: type[ProtocolAdapter]):
        """注册协议适配器"""
        cls._REGISTRY[protocol.lower()] = adapter_class

    @classmethod
    def create(cls, protocol: str, config: dict) -> ProtocolAdapter:
        """创建协议适配器实例

        Args:
            protocol: 协议类型 (rest/soap/graphql/websocket/grpc)
            config: 连接配置

        Returns:
            ProtocolAdapter 实例

        Raises:
            ValueError: 不支持的协议类型
        """
        protocol = protocol.lower()
        if protocol not in cls._REGISTRY:
            raise ValueError(
                f"不支持的协议类型: {protocol}，"
                f"支持: {list(cls._REGISTRY.keys())}"
            )
        return cls._REGISTRY[protocol](config)

    @classmethod
    def get_supported_protocols(cls) -> list[str]:
        """获取已注册的协议列表"""
        return list(cls._REGISTRY.keys())


# 注册内置适配器
def _register_builtin_adapters():
    from app.adapters.rest import RestAdapter
    ProtocolAdapterFactory.register("rest", RestAdapter)
    ProtocolAdapterFactory.register("http", RestAdapter)

    try:
        from app.adapters.soap import SoapAdapter
        ProtocolAdapterFactory.register("soap", SoapAdapter)
    except ImportError:
        pass

    try:
        from app.adapters.graphql import GraphqlAdapter
        ProtocolAdapterFactory.register("graphql", GraphqlAdapter)
    except ImportError:
        pass

    try:
        from app.adapters.websocket import WebsocketAdapter
        ProtocolAdapterFactory.register("websocket", WebsocketAdapter)
    except ImportError:
        pass

    try:
        from app.adapters.grpc_adapter import GrpcAdapter
        ProtocolAdapterFactory.register("grpc", GrpcAdapter)
    except ImportError:
        pass

    try:
        from app.adapters.dubbo import DubboAdapter
        ProtocolAdapterFactory.register("dubbo", DubboAdapter)
    except ImportError:
        pass

    try:
        from app.adapters.hap import HapV3Adapter
        ProtocolAdapterFactory.register("hap", HapV3Adapter)
        ProtocolAdapterFactory.register("hapv3", HapV3Adapter)
    except ImportError:
        pass


_register_builtin_adapters()
