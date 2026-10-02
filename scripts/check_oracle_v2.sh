#!/usr/bin/env bash
# Oracle 就绪检查 v2：完整诊断输出 + 循环轮询
# 用法: wsl -- bash /mnt/e/GPOST/scripts/check_oracle_v2.sh
LOG=/tmp/oracle_check.log
: > $LOG
for i in $(seq 1 40); do
  docker exec apibridge-oracle-test bash -c \
    'export ORACLE_HOME=/u01/app/oracle/product/11.2.0/xe; export PATH="$ORACLE_HOME/bin:$PATH"; echo "SELECT 1 AS OK FROM DUAL;" | sqlplus -s system/oracle@localhost:1521/XE' \
    >> $LOG 2>&1
  if grep -qE '^[[:space:]]*1[[:space:]]*$' $LOG 2>/dev/null; then
    echo "READY attempt=$i"
    grep -v "ORA-28002" $LOG | tail -5
    exit 0
  fi
  echo "--- attempt=$i last output:"
  tail -3 $LOG
  : > $LOG
  sleep 10
done
echo "TIMEOUT"
exit 1
