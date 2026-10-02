#!/usr/bin/env bash
# 在测试容器内执行 SQL（用法: bash ora_query.sh "SELECT ...")
export ORACLE_HOME=/u01/app/oracle/product/11.2.0/xe
export PATH="$ORACLE_HOME/bin:$PATH"
echo "$1;" | sqlplus -s system/oracle@localhost:1521/XE 2>&1 | grep -v '^$'
