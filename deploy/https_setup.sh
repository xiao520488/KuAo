#!/bin/bash
# 全站 HTTPS 一键部署（可重复执行，失败自动回滚 nginx 配置）
# 前置：/tmp 下已有 www.kuao.xyz.pem、www.kuao.xyz.key、nginx-kuao.xyz.conf
# 用法：ssh kuao "bash /tmp/https_setup.sh"
set -euo pipefail

CERT_DIR=/etc/nginx/certs
CONF=/etc/nginx/sites-available/default
ENV_FILE=/root/KuAo/backend/.env
TS=$(date +%Y%m%d-%H%M%S)

echo "== 1/6 证书落位 =="
install -d -m 700 "$CERT_DIR"
install -m 644 -o root -g root /tmp/www.kuao.xyz.pem "$CERT_DIR/www.kuao.xyz.pem"
install -m 600 -o root -g root /tmp/www.kuao.xyz.key "$CERT_DIR/www.kuao.xyz.key"
ls -l "$CERT_DIR"
openssl x509 -noout -subject -enddate -in "$CERT_DIR/www.kuao.xyz.pem"

echo "== 2/6 备份现有配置 =="
cp -a "$CONF" "/root/nginx-default.bak-$TS"
cp -a "$ENV_FILE" "/root/backend-env.bak-$TS"
echo "已备份：/root/nginx-default.bak-$TS /root/backend-env.bak-$TS"

echo "== 3/6 安装 nginx 站点配置 =="
install -m 644 /tmp/nginx-kuao.xyz.conf "$CONF"
if ! nginx -t; then
    echo "!! nginx -t 失败，回滚配置，站点保持原状"
    cp -a "/root/nginx-default.bak-$TS" "$CONF"
    exit 1
fi

echo "== 4/6 更新后端 CORS =="
sed -i 's|^CORS_ALLOW_ORIGINS=.*|CORS_ALLOW_ORIGINS="https://www.kuao.xyz,https://kuao.xyz"|' "$ENV_FILE"
grep -n '^CORS_ALLOW_ORIGINS' "$ENV_FILE"

echo "== 5/6 reload nginx + 重启后端 =="
systemctl reload nginx
systemctl restart kuao-backend
sleep 3

echo "== 6/6 验收 =="
code80=$(curl -sS -o /dev/null -w '%{http_code}' -H 'Host: www.kuao.xyz' http://127.0.0.1/ || true)
loc80=$(curl -sS -o /dev/null -w '%{redirect_url}' -H 'Host: www.kuao.xyz' http://127.0.0.1/ || true)
code443=$(curl -sS --resolve www.kuao.xyz:443:127.0.0.1 -o /dev/null -w '%{http_code}' https://www.kuao.xyz/ || true)
codeapi=$(curl -sS --resolve www.kuao.xyz:443:127.0.0.1 -o /dev/null -w '%{http_code}' 'https://www.kuao.xyz/api/competitions?limit=1' || true)
codebare=$(curl -sS --resolve kuao.xyz:443:127.0.0.1 -o /dev/null -w '%{http_code}' https://kuao.xyz/ || true)

echo "HTTP  : $code80 -> $loc80   （期望 301 -> https://www.kuao.xyz/）"
echo "HTTPS : 站点 $code443 / API $codeapi / 裸域 $codebare   （站点与 API 期望 200，裸域期望 301）"
curl -sS --resolve kuao.xyz:443:127.0.0.1 -o /dev/null -w '裸域跳转 -> %{http_code} %{redirect_url}\n' https://kuao.xyz/ || true
curl -sS --resolve www.kuao.xyz:443:127.0.0.1 -o /dev/null -D - -H 'Origin: https://www.kuao.xyz' \
    'https://www.kuao.xyz/api/competitions?limit=1' | grep -i '^access-control-allow-origin' \
    || echo "CORS  : 未回包（同源请求不带 Origin，属正常）"
systemctl is-active nginx kuao-backend

if [ "$code80" != "301" ] || [ "$code443" != "200" ] || [ "$codeapi" != "200" ] || [ "$codebare" != "301" ]; then
    echo "!! 验收未通过，回滚 nginx 配置并 reload"
    cp -a "/root/nginx-default.bak-$TS" "$CONF"
    nginx -t && systemctl reload nginx
    exit 1
fi
echo "== 完成：https://www.kuao.xyz 已可用 =="