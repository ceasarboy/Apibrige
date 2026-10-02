#!/usr/bin/env bash
# 启动 Oracle XE 11g 测试容器（目标系统同款配置：1521 / SID XE / system+oracle）
set -u
docker rm -f apibridge-oracle-test 2>/dev/null
docker volume rm -f apibridge-oracle-data 2>/dev/null
docker rmi registry.cn-hangzhou.aliyuncs.com/qida/oracle-xe-11g:latest 2>/dev/null
docker run -d --name apibridge-oracle-test \
  -p 1521:1521 \
  -e ORACLE_DISABLE_ASYNCH_IO=true \
  -e ORACLE_ALLOW_REMOTE=true \
  -v apibridge-oracle-data:/u01/app/oracle \
  qida/oracle-xe-11g:latest
echo "container_id=$(docker inspect -f '{{.State.Status}}' apibridge-oracle-test)"
