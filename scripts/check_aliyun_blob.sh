#!/usr/bin/env bash
# 检查 qida 缺失 blob 在阿里云的真实状态（带匿名 token）
set -u
TOKEN_JSON=$(curl -s --max-time 10 'https://dockerauth.cn-hangzhou.aliyuncs.com/auth?service=registry.aliyuncs.com:cn-hangzhou:26842&scope=repository:qida/oracle-xe-11g:pull')
echo "auth_response_head: $(echo "$TOKEN_JSON" | head -c 120)"
T=$(echo "$TOKEN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)
echo "token_len=${#T}"
for b in f069f1d2105921f2b5520c0d1d18b04a5fe2e10f9a21afe9661cf9939c287e09 4ea53ad65b45c7b23c316c7c1ada63909385b2cc4551df6924b2ed910107dc4b; do
  code=$(curl -s -o /dev/null --max-time 15 -H "Authorization: Bearer $T" -w '%{http_code}' -I "https://registry.cn-hangzhou.aliyuncs.com/v2/qida/oracle-xe-11g/blobs/sha256:$b")
  echo "blob $b -> $code"
done
