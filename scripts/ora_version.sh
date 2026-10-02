#!/usr/bin/env bash
export ORACLE_HOME=/u01/app/oracle/product/11.2.0/xe
export PATH="$ORACLE_HOME/bin:$PATH"
sqlplus -s system/oracle@localhost:1521/XE <<'EOF' 2>&1 | grep -v '^$' | grep -v 'ORA-28002'
SET PAGESIZE 100
SELECT banner FROM v$version;
SELECT instance_name, status FROM v$instance;
EOF
