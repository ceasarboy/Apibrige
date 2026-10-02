"""便携包入口：API 与编译后的前端共用一个本地端口。"""
from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from app.main import app


class FrontendFiles(StaticFiles):
    async def get_response(self, path, scope):
        path = path.replace('\\', '/').lstrip('/')
        try:
            return await super().get_response(path, scope)
        except HTTPException as error:
            if error.status_code != 404 or path.split('/')[0] in ('api', 'assets') or Path(path).suffix or path.startswith('.'):
                raise
            return FileResponse(Path(self.directory) / 'index.html')


app.mount('/', FrontendFiles(directory=Path(__file__).parent / 'web', html=True), name='frontend')
