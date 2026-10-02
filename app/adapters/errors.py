"""可跨同步/异步/探针传递的传输错误。"""
class AdapterError(ValueError):
    def __init__(self, code: str, message: str, stage: str = 'request_build', *, details: dict | None = None):
        self.code = code
        self.stage = stage
        self.details = details or {}
        super().__init__(f'{code}: {message}')

    def as_dict(self):
        from app.security.desensitize import desensitize_record
        return desensitize_record({'code': self.code, 'stage': self.stage, 'message': str(self), **self.details})
