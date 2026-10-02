"""REST 翻页状态与结束判断，不发起请求、不返回未确认完成的部分数据。"""
import hashlib
import json

from app.adapters.errors import AdapterError
from app.services.rest_contract import pointer_get


def pagination_error(message):
    raise AdapterError('source_pagination_invalid', message, 'source_read')


class SourcePagination:
    def __init__(self, config):
        self.config = config
        self.page = config.page_start
        self.cursor = config.cursor_start
        self.seen_cursors = {(type(self.cursor), self.cursor)} if self.cursor is not None else set()
        self.seen_pages = set()
        self.received = 0
        self.total = None

    def parameters(self):
        cfg = self.config
        if cfg.mode == 'none':
            return {}
        params = {cfg.size_param: cfg.page_size} if cfg.size_param else {}
        if cfg.mode == 'page':
            params[cfg.page_param] = self.page
        elif self.cursor is not None:
            params[cfg.cursor_param] = self.cursor
        return params

    def finished(self, body, rows):
        cfg = self.config
        if cfg.mode == 'none':
            return True
        if len(rows) > cfg.page_size:
            pagination_error('源接口返回的单页条数超过配置，请检查每页条数参数')
        if rows:
            canonical = sorted(json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False) for row in rows)
            digest = hashlib.sha256(json.dumps(canonical).encode()).digest()
            if digest in self.seen_pages:
                pagination_error('源接口重复返回相同页，请检查分页参数与排序')
            self.seen_pages.add(digest)
        self.received += len(rows)
        if cfg.mode == 'cursor':
            cursor = pointer_get(body, cfg.next_cursor_path)
            if cursor is None or cursor == '':
                return True
            if type(cursor) not in (str, int):
                pagination_error('下一游标字段缺失或类型无效，不能确认读取完成')
            identity = (type(cursor), cursor)
            if identity in self.seen_cursors:
                pagination_error('下一游标重复，已停止循环读取')
            self.seen_cursors.add(identity)
            self.cursor = cursor
            done = False
        elif cfg.end_condition == 'total':
            total = pointer_get(body, cfg.total_path)
            if type(total) is not int or total < 0:
                pagination_error('总记录数缺失或不是非负整数')
            if self.total is not None and total != self.total:
                pagination_error('分页期间总记录数变化，请待源稳定后重试')
            self.total = total
            if self.received > total:
                pagination_error('已读取记录数超过源声明总数')
            done = self.received == total
        elif cfg.end_condition == 'has_more':
            more = pointer_get(body, cfg.has_more_path)
            if type(more) is not bool:
                pagination_error('是否有下一页字段缺失或不是布尔值')
            done = not more
        else:
            done = len(rows) < cfg.page_size
        if not done and not rows:
            pagination_error('源返回空页但仍声明有后续数据，不能确认读取完整')
        self.page += 1
        return done
