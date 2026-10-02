"""轮询等待 Oracle 测试库就绪"""
import sys
import time

import oracledb

sys.path.insert(0, ".")

for attempt in range(30):
    try:
        conn = oracledb.connect(
            user="system", password="oracle",
            dsn="localhost:1521/XE",
        )
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM DUAL")
            cur.fetchall()
        conn.close()
        print(f"ORACLE_READY (attempt {attempt + 1})")
        sys.exit(0)
    except Exception as e:
        print(f"attempt {attempt + 1}: {e}")
        time.sleep(5)
print("ORACLE_NOT_READY")
sys.exit(1)
