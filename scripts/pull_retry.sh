#!/usr/bin/env bash
# gvenzl/oracle-xe 11g 双镜像源断点续传拉取（CF RST 环境下的重试循环）
# 用法: wsl -d Ubuntu -- bash /mnt/e/GPOST/scripts/pull_retry.sh
set -u
MS="docker.1ms.run/gvenzl/oracle-xe:11.2.0.2-faststart"
P1="docker.1panel.live/gvenzl/oracle-xe:11.2.0.2-faststart"

for i in $(seq 1 120); do
  docker pull "$MS" > /tmp/pull_ms.log 2>&1
  rc=$?
  if [ $rc -eq 0 ]; then
    echo "MS_OK attempt=$i"
    docker tag "$MS" gvenzl-11g:local
    break
  fi
  echo "ms attempt=$i $(tail -1 /tmp/pull_ms.log | cut -c1-140)"
  sleep 3
done

if docker images --format '{{.Repository}}' | grep -q gvenzl; then
  echo "DONE_HAVE_IMAGE"
  docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep -E 'gvenzl|gvenzl-11g'
  exit 0
fi

for i in $(seq 1 120); do
  docker pull "$P1" > /tmp/pull_1p.log 2>&1
  rc=$?
  if [ $rc -eq 0 ]; then
    echo "P1_OK attempt=$i"
    docker tag "$P1" gvenzl-11g:local
    break
  fi
  echo "1p attempt=$i $(tail -1 /tmp/pull_1p.log | cut -c1-140)"
  sleep 5
done

echo "=== final ==="
docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep -E 'gvenzl|gvenzl-11g' || echo "NO_IMAGE_YET"
