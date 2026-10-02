#!/usr/bin/env bash
# 轮询等待容器内 Oracle 就绪（sqlplus SELECT 1 成功即就绪）
for i in $(seq 1 40); do
  if docker exec apibridge-oracle-test bash -c \
    'export ORACLE_HOME=/u01/app/oracle/product/11.2.0/xe; export PATH="$ORACLE_HOME/bin:$PATH"; echo "SELECT 1 FROM DUAL;" | sqlplus -s system/oracle@localhost:1521/XE' 2>/dev/null \
    | grep -q '^ *1 *$'; then
    echo "ORACLE_READY attempt=$i"
    # 保持 WSL 存活（防止空闲超时杀掉容器），给 Windows 侧测试留 20 分钟窗口
    echo "KEEPALIVE 1200s"
    sleep 1200
    exit 0
  fi
  sleep 10
done
echo "ORACLE_NOT_READY_TIMEOUT"
docker logs --tail 15 apibridge-oracle-test 2>&1
exit 1
