#!/usr/bin/env bash
# 用 skopeo 绕开 containerd GC 竞态：直接下载 qida/oracle-xe-11g 为 docker-archive 再 load
set -eu
export DEBIAN_FRONTEND=noninteractive

if ! command -v skopeo >/dev/null 2>&1; then
  echo "[1] 安装 skopeo ..."
  apt-get update -qq
  apt-get install -y -qq skopeo >/dev/null
fi
skopeo --version

echo "[2] skopeo copy -> /mnt/e/tmp/oracle-xe-11g.tar（阿里云线路直下）"
mkdir -p /mnt/e/tmp
skopeo copy --retry-times 5 \
  docker://registry.cn-hangzhou.aliyuncs.com/qida/oracle-xe-11g:latest \
  docker-archive:/mnt/e/tmp/oracle-xe-11g.tar:qida/oracle-xe-11g:latest

echo "[3] docker load"
docker load -i /mnt/e/tmp/oracle-xe-11g.tar

echo "[4] 校验"
docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep -E 'oracle-xe|oracle'
echo IMAGE_READY
