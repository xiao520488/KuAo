#!/bin/bash
# 全站 HTTPS 后的 Cookie 加固：刷新令牌只走加密连接
set -e
cd /root/KuAo/backend
cp -a .env "/root/backend-env.bak-$(date +%Y%m%d-%H%M%S)"

if grep -q '^COOKIE_SECURE=' .env; then
    sed -i 's|^COOKIE_SECURE=.*|COOKIE_SECURE=1|' .env
else
    printf '\n# 全站 HTTPS：刷新令牌 Cookie 仅通过加密连接传输\nCOOKIE_SECURE=1\n' >> .env
fi
grep -n 'COOKIE_SECURE' .env

systemctl restart kuao-backend
sleep 3

echo '--- 登录响应头（应含 Secure; HttpOnly） ---'
curl -s -i -X POST https://www.kuao.xyz/api/auth/login \
    --resolve www.kuao.xyz:443:127.0.0.1 \
    -H 'Content-Type: application/json' \
    -d '{"username":"xas","password":"123456"}' | grep -i '^set-cookie'

echo '--- 无 Cookie 时刷新应 401 ---'
curl -s -o /dev/null -w 'refresh(no cookie) -> %{http_code}\n' -X POST \
    https://www.kuao.xyz/api/auth/refresh --resolve www.kuao.xyz:443:127.0.0.1

echo '--- 服务状态 ---'
systemctl is-active kuao-backend