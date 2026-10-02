"""连接器 delete_rows：按键删除目标表行（删除同步，task 2.2 相关）"""
from pathlib import Path

p = Path("app/connectors/database.py")
src = p.read_text(encoding="utf-8")

# 1. 抽象基类加 delete_rows
old = '''    @abstractmethod
    async def test_connection(self) -> dict:'''
new = '''    @abstractmethod
    async def delete_rows(self, table: str, key_field: str,
                          values: list) -> int:
        """按键删除行：DELETE FROM table WHERE key_field IN (values)

        Returns:
            删除行数
        """
        ...

    @abstractmethod
    async def test_connection(self) -> dict:'''
assert old in src, "abstract"
src = src.replace(old, new)

# 2. SQLite
old = '''    async def test_connection(self) -> dict:
        def _do():
            t0 = time.perf_counter()
            try:
                conn = self._connect_sync()
                try:
                    conn.execute("SELECT 1")
                finally:
                    conn.close()
                latency = int((time.perf_counter() - t0) * 1000)
                return {"status": "ok", "latency_ms": latency, "error": None}
            except Exception as e:
                latency = int((time.perf_counter() - t0) * 1000)
                return {"status": "failed", "latency_ms": latency, "error": str(e)}

        return await self._call(_do)'''
new = '''    async def delete_rows(self, table: str, key_field: str, values: list) -> int:
        t = _validate_identifier(table)
        kf = _validate_identifier(key_field)
        vals = [v for v in values if v is not None]
        if not vals:
            return 0
        placeholders = ", ".join("?" for _ in vals)

        def _do():
            conn = self._connect_sync()
            try:
                cur = conn.execute(
                    f'DELETE FROM "{t}" WHERE "{kf}" IN ({placeholders})', vals)
                n = cur.rowcount
                conn.commit()
                return n
            finally:
                conn.close()

        return await self._call(_do)

    async def test_connection(self) -> dict:
        def _do():
            t0 = time.perf_counter()
            try:
                conn = self._connect_sync()
                try:
                    conn.execute("SELECT 1")
                finally:
                    conn.close()
                latency = int((time.perf_counter() - t0) * 1000)
                return {"status": "ok", "latency_ms": latency, "error": None}
            except Exception as e:
                latency = int((time.perf_counter() - t0) * 1000)
                return {"status": "failed", "latency_ms": latency, "error": str(e)}

        return await self._call(_do)'''
assert old in src, "sqlite delete"
src = src.replace(old, new)

# 3. PostgreSQL
old = '''    async def test_connection(self) -> dict:
        start = time.perf_counter()
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            latency = int((time.perf_counter() - start) * 1000)
            return {"status": "ok", "latency_ms": latency, "error": None}
        except Exception as e:
            latency = int((time.perf_counter() - start) * 1000)
            return {"status": "failed", "latency_ms": latency, "error": str(e)}

    async def disconnect(self):
        self._pool = None  # 池归注册表持有'''
new = '''    async def delete_rows(self, table: str, key_field: str, values: list) -> int:
        t = _validate_identifier(table)
        kf = _validate_identifier(key_field)
        vals = [v for v in values if v is not None]
        if not vals:
            return 0
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                f'DELETE FROM "{t}" WHERE "{kf}" = ANY($1)', vals)
        return int(result.split()[-1])

    async def test_connection(self) -> dict:
        start = time.perf_counter()
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            latency = int((time.perf_counter() - start) * 1000)
            return {"status": "ok", "latency_ms": latency, "error": None}
        except Exception as e:
            latency = int((time.perf_counter() - start) * 1000)
            return {"status": "failed", "latency_ms": latency, "error": str(e)}

    async def disconnect(self):
        self._pool = None  # 池归注册表持有'''
assert old in src, "pg delete"
src = src.replace(old, new)

# 4. MySQL
old = '''    async def test_connection(self) -> dict:
        start = time.perf_counter()
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1")
            latency = int((time.perf_counter() - start) * 1000)
            return {"status": "ok", "latency_ms": latency, "error": None}
        except Exception as e:
            latency = int((time.perf_counter() - start) * 1000)
            return {"status": "failed", "latency_ms": latency, "error": str(e)}

    async def disconnect(self):
        self._pool = None  # 池归注册表持有'''
new = '''    async def delete_rows(self, table: str, key_field: str, values: list) -> int:
        t = _validate_identifier(table)
        kf = _validate_identifier(key_field)
        vals = [v for v in values if v is not None]
        if not vals:
            return 0
        placeholders = ", ".join("%s" for _ in vals)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"DELETE FROM `{t}` WHERE `{kf}` IN ({placeholders})", vals)
                return cur.rowcount

    async def test_connection(self) -> dict:
        start = time.perf_counter()
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1")
            latency = int((time.perf_counter() - start) * 1000)
            return {"status": "ok", "latency_ms": latency, "error": None}
        except Exception as e:
            latency = int((time.perf_counter() - start) * 1000)
            return {"status": "failed", "latency_ms": latency, "error": str(e)}

    async def disconnect(self):
        self._pool = None  # 池归注册表持有'''
assert old in src, "mysql delete"
src = src.replace(old, new)

# 5. Oracle
old = '''    async def test_connection(self) -> dict:
        start = time.perf_counter()
        try:
            pool = await self._get_pool()

            def _do():
                with pool.acquire() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute("SELECT 1 FROM DUAL")
                        cursor.fetchall()

            await asyncio.to_thread(_do)
            latency = int((time.perf_counter() - start) * 1000)
            return {"status": "ok", "latency_ms": latency, "error": None}
        except Exception as e:
            latency = int((time.perf_counter() - start) * 1000)
            return {"status": "failed", "latency_ms": latency, "error": str(e)}'''
new = '''    async def delete_rows(self, table: str, key_field: str, values: list) -> int:
        t = _validate_identifier(table).upper()
        kf = _validate_identifier(key_field).upper()
        vals = [v for v in values if v is not None]
        if not vals:
            return 0
        binds = ", ".join(f":{i+1}" for i in range(len(vals)))
        sql = f'DELETE FROM "{t}" WHERE "{kf}" IN ({binds})'
        pool = await self._get_pool()

        def _do():
            with pool.acquire() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(sql, vals)
                    deleted = cursor.rowcount
                conn.commit()
            return deleted

        return await asyncio.to_thread(_do)

    async def test_connection(self) -> dict:
        start = time.perf_counter()
        try:
            pool = await self._get_pool()

            def _do():
                with pool.acquire() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute("SELECT 1 FROM DUAL")
                        cursor.fetchall()

            await asyncio.to_thread(_do)
            latency = int((time.perf_counter() - start) * 1000)
            return {"status": "ok", "latency_ms": latency, "error": None}
        except Exception as e:
            latency = int((time.perf_counter() - start) * 1000)
            return {"status": "failed", "latency_ms": latency, "error": str(e)}'''
assert old in src, "oracle delete"
src = src.replace(old, new)

p.write_text(src, encoding="utf-8", newline="\n")
print("delete_rows added to all connectors")
