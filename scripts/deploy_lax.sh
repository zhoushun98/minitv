#!/usr/bin/env bash
# MiniTV 全栈部署（幂等）：API + 官网 + H5 + 管理后台全部跑在同一台服务器上。
# Cloudflare 只做 DNS 解析（灰云），HTTPS 证书由服务器上的 Let's Encrypt 签发。
#
# 只允许经发布脚本调用：
#   python3 scripts/release.py run --target lax -- ./scripts/deploy_lax.sh
#
# 域名布局（DOMAIN 默认 sanaoman.asia）：
#   DOMAIN         官网        → $WEB_ROOT/web
#   www.DOMAIN     301 → DOMAIN
#   h5.DOMAIN      H5          → $WEB_ROOT/h5
#   admin.DOMAIN   管理后台    → $WEB_ROOT/admin-root/admin（vite base 为 /admin/）
#   api.DOMAIN     FastAPI     → 127.0.0.1:$API_PORT
#
# 生产密钥只在服务器上生成，保存在 $REMOTE_DIR/.env，本机的 .env 永远不会被复制上去。
# 以后要补 Bunny / Resend / Stripe 等密钥：编辑服务器上的 .env，再 systemctl restart minitv-api。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${MINITV_DEPLOY_HOST:-lax}"
DOMAIN="${MINITV_DOMAIN:-sanaoman.asia}"
REMOTE_DIR="${MINITV_REMOTE_DIR:-/opt/minitv}"
WEB_ROOT="${MINITV_WEB_ROOT:-/var/www/minitv}"
API_PORT="${MINITV_API_PORT:-18110}"

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=15 -o ServerAliveCountMax=3)
RSYNC_SSH="ssh -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=15 -o ServerAliveCountMax=3"
REMOTE_ENV="DOMAIN='$DOMAIN' REMOTE_DIR='$REMOTE_DIR' WEB_ROOT='$WEB_ROOT' API_PORT='$API_PORT'"

remote() { ssh "${SSH_OPTS[@]}" "$HOST" "$@"; }

# ---------------------------------------------------------------- 1. 本地构建前端
# 各前端 .env.production 已指向 sanaoman.asia；这里再按 DOMAIN 显式传一遍，换域名时只改 DOMAIN 即可。
build() {
  local dir="$1"
  shift
  echo "→ build $dir"
  (
    cd "$ROOT/$dir"
    [[ -d node_modules ]] || npm ci --no-audit --no-fund
    env "$@" npm run build
  )
  if ! grep -q "api\.$DOMAIN" "$ROOT/$dir"/dist/assets/*.js; then
    echo "构建自检失败：$dir/dist 里找不到 api.$DOMAIN" >&2
    exit 1
  fi
}

API_BASE="https://api.$DOMAIN"
build web VITE_API_BASE="$API_BASE" VITE_WEB_APP_URL="https://h5.$DOMAIN" VITE_ADMIN_URL="https://admin.$DOMAIN"
build h5 VITE_API_BASE="$API_BASE"
build admin VITE_API_BASE="$API_BASE"
if ! grep -q "h5\.$DOMAIN" "$ROOT"/web/dist/assets/*.js; then
  echo "构建自检失败：web/dist 里找不到 h5.$DOMAIN" >&2
  exit 1
fi

# ---------------------------------------------------------------- 2. 服务器装包、建目录
echo "→ bootstrap packages on $HOST"
remote "$REMOTE_ENV bash -s" << 'REMOTE'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
need=()
for pkg in nginx postgresql redis-server python3-venv rsync certbot curl openssl; do
  dpkg -s "$pkg" > /dev/null 2>&1 || need+=("$pkg")
done
if ((${#need[@]})); then
  apt-get update -qq
  apt-get install -y -qq "${need[@]}"
fi
install -d -m 755 "$REMOTE_DIR" "$WEB_ROOT/web/download" "$WEB_ROOT/web/cli" "$WEB_ROOT/h5" \
  "$WEB_ROOT/admin-root/admin" /var/www/letsencrypt
REMOTE

# ---------------------------------------------------------------- 3. 同步代码与静态产物
echo "→ sync backend + static sites to $HOST"
rsync -az --delete -e "$RSYNC_SSH" \
  --exclude '.env' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude '*.pyc' \
  "$ROOT/backend/" "$HOST:$REMOTE_DIR/backend/"
# download/（手工放的 APK）和 cli/（下面单独同步）不属于 web 构建产物，排除后 --delete 不会删掉它们。
rsync -az --delete -e "$RSYNC_SSH" --exclude 'download/' --exclude 'cli/' "$ROOT/web/dist/" "$HOST:$WEB_ROOT/web/"
# 后台「API Key」页给出的安装命令是 curl https://$DOMAIN/cli/install.sh | sh。
rsync -az -e "$RSYNC_SSH" "$ROOT/cli/install.sh" "$ROOT/cli/minitv" "$HOST:$WEB_ROOT/web/cli/"
rsync -az --delete -e "$RSYNC_SSH" "$ROOT/h5/dist/" "$HOST:$WEB_ROOT/h5/"
rsync -az --delete -e "$RSYNC_SSH" "$ROOT/admin/dist/" "$HOST:$WEB_ROOT/admin-root/admin/"

# ---------------------------------------------------------------- 4. 服务器配置并启动
echo "→ configure runtime on $HOST"
remote "$REMOTE_ENV bash -s" << 'REMOTE'
set -euo pipefail
chmod -R a+rX "$WEB_ROOT"

# 4.1 生产 .env：只在不存在时生成，已有的绝不覆盖（里面有数据库口令和签名密钥）。
if [[ ! -f "$REMOTE_DIR/.env" ]]; then
  python3 - "$REMOTE_DIR/.env" << 'PY'
import os
import secrets
import sys
from pathlib import Path

d = os.environ["DOMAIN"]
port = os.environ["API_PORT"]
pg = secrets.token_urlsafe(24)
rd = secrets.token_urlsafe(24)


def key() -> str:
    return secrets.token_urlsafe(32)


text = f"""# MiniTV 生产环境变量（{d}）。在服务器上生成，不从本机复制。
APP_ENV=production
API_HOST=127.0.0.1
API_PORT={port}

POSTGRES_USER=minitv
POSTGRES_PASSWORD={pg}
POSTGRES_DB=minitv
DATABASE_URL=postgresql://minitv:{pg}@127.0.0.1:5432/minitv
REDIS_PASSWORD={rd}
REDIS_URL=redis://:{rd}@127.0.0.1:6379/0

CORS_ORIGINS=https://{d},https://www.{d},https://h5.{d},https://admin.{d}
SITE_DOMAIN={d}
SITE_URL=https://{d}
WWW_URL=https://www.{d}
API_BASE_URL=https://api.{d}
ADMIN_URL=https://admin.{d}
H5_PUBLIC_BASE_URL=https://h5.{d}

# 签名密钥：随机生成，上线后不要随意更换（MODERATION_KEY_SECRET 换了会解不开后台保存的密钥）。
PLAYBACK_TOKEN_KEY={key()}
IMAGE_TOKEN_KEY={key()}
FEED_CURSOR_KEY={key()}
PAY_WEBHOOK_SECRET={key()}
BUNNY_CALLBACK_SECRET={key()}
MODERATION_KEY_SECRET={key()}

# ---- 第三方服务：填好后执行 systemctl restart minitv-api ----
# 视频（Bunny）：fake 模式下不能真正上传和播放。切 real 前要把下面的 BUNNY_* 全部填好（说明见仓库 .env.example）。
BUNNY_MODE=fake
# BUNNY_API_KEY=
# BUNNY_STORAGE_ZONE=
# BUNNY_STORAGE_HOST=
# BUNNY_STORAGE_PASSWORD=
# BUNNY_STORAGE_READONLY_PASSWORD=
# BUNNY_IMAGE_PULL_ZONE_ID=
# BUNNY_IMAGE_CDN_HOST=
# BUNNY_IMAGE_TOKEN_KEY=
# BUNNY_STREAM_LIBRARY_ID=
# BUNNY_STREAM_API_KEY=
# BUNNY_STREAM_READONLY_API_KEY=
# BUNNY_STREAM_PULL_ZONE_ID=
# BUNNY_STREAM_CDN_HOST=
# BUNNY_STREAM_TOKEN_KEY=
# 邮件（Resend）：注册 / 登录验证码。RESEND_FROM 必须是 Resend 里验证过的域名。
# RESEND_API_KEY=
# RESEND_FROM=
# 支付（Stripe）：会员订阅、购买金币。
# STRIPE_SECRET_KEY=
# STRIPE_WEBHOOK_SECRET=
# 第三方登录、自动审核
# GOOGLE_CLIENT_ID=
# FACEBOOK_APP_ID=
# FACEBOOK_APP_SECRET=
# OPENAI_API_KEY=
"""
path = Path(sys.argv[1])
path.write_text(text)
path.chmod(0o600)
print(f"installed new production env at {path}")
PY
else
  echo "keeping existing production env"
fi
set -a
# shellcheck disable=SC1091
source "$REMOTE_DIR/.env"
set +a

# 4.2 PostgreSQL / Redis
systemctl enable --now postgresql
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -q << SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'minitv') THEN
    CREATE ROLE minitv LOGIN PASSWORD '${POSTGRES_PASSWORD}';
  ELSE
    ALTER ROLE minitv WITH LOGIN PASSWORD '${POSTGRES_PASSWORD}';
  END IF;
END
\$\$;
SELECT 'CREATE DATABASE minitv OWNER minitv'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'minitv')\gexec
GRANT ALL PRIVILEGES ON DATABASE minitv TO minitv;
SQL
runuser -u postgres -- psql -d minitv -v ON_ERROR_STOP=1 -q -c "GRANT ALL ON SCHEMA public TO minitv;"

REDIS_CONF=/etc/redis/redis.conf
if grep -q '^requirepass ' "$REDIS_CONF"; then
  sed -i "s/^requirepass .*/requirepass ${REDIS_PASSWORD}/" "$REDIS_CONF"
else
  printf '\nrequirepass %s\n' "$REDIS_PASSWORD" >> "$REDIS_CONF"
fi
sed -i 's/^bind .*/bind 127.0.0.1 -::1/' "$REDIS_CONF"
systemctl enable redis-server
systemctl restart redis-server

# 4.3 Python 运行时、迁移、首个后台账号
python3 -m venv "$REMOTE_DIR/.venv"
"$REMOTE_DIR/.venv/bin/pip" install -q --disable-pip-version-check -r "$REMOTE_DIR/backend/requirements.txt"
(
  cd "$REMOTE_DIR/backend"
  ../.venv/bin/alembic upgrade head
  # 只在 admin_users 为空时创建 admin，并把随机初始密码打印一次。
  ../.venv/bin/python -m app.seed_admin
)

# 4.4 systemd
cat > /etc/systemd/system/minitv-api.service << UNIT
[Unit]
Description=MiniTV API
After=network.target postgresql.service redis-server.service
Wants=postgresql.service redis-server.service

[Service]
Type=simple
WorkingDirectory=$REMOTE_DIR
EnvironmentFile=$REMOTE_DIR/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$REMOTE_DIR/.venv/bin/uvicorn app.main:app --app-dir $REMOTE_DIR/backend --host 127.0.0.1 --port $API_PORT --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable minitv-api
systemctl restart minitv-api

# 4.5 Nginx：先上 80 端口（ACME 验证 + 跳转 https），拿到证书后再写 443。
# 本机内核禁用了 IPv6，DNS 也没有 AAAA 记录，所以只监听 IPv4。
NAMES=("$DOMAIN" "www.$DOMAIN" "h5.$DOMAIN" "admin.$DOMAIN" "api.$DOMAIN")
CERT_DIR="/etc/letsencrypt/live/$DOMAIN"

cat > /etc/nginx/sites-available/minitv-http << NGINX
server {
    listen 80 default_server;
    server_name _;
    location ^~ /.well-known/acme-challenge/ { root /var/www/letsencrypt; default_type text/plain; }
    location / { return 444; }
}

server {
    listen 80;
    server_name ${NAMES[*]};
    location ^~ /.well-known/acme-challenge/ { root /var/www/letsencrypt; default_type text/plain; }
    location / { return 301 https://\$host\$request_uri; }
}
NGINX

if ! grep -Eq '^\s*gzip_types' /etc/nginx/nginx.conf; then
  cat > /etc/nginx/conf.d/minitv-gzip.conf << 'NGINX'
gzip_vary on;
gzip_proxied any;
gzip_types text/plain text/css application/javascript application/json image/svg+xml;
NGINX
fi

rm -f /etc/nginx/sites-enabled/default
ln -sfn /etc/nginx/sites-available/minitv-http /etc/nginx/sites-enabled/minitv-http
nginx -t
systemctl enable nginx
systemctl reload nginx || systemctl restart nginx

# 已有证书且域名一致时 certbot 会直接跳过，不会重复申请。
domain_args=()
for name in "${NAMES[@]}"; do domain_args+=(-d "$name"); done
certbot certonly --webroot -w /var/www/letsencrypt --cert-name "$DOMAIN" "${domain_args[@]}" \
  --non-interactive --agree-tos --register-unsafely-without-email \
  --keep-until-expiring --expand --deploy-hook "systemctl reload nginx"
systemctl enable --now certbot.timer

cat > /etc/nginx/snippets/minitv-ssl.conf << NGINX
listen 443 ssl;
http2 on;
ssl_certificate $CERT_DIR/fullchain.pem;
ssl_certificate_key $CERT_DIR/privkey.pem;
ssl_protocols TLSv1.2 TLSv1.3;
ssl_session_cache shared:minitv_ssl:10m;
ssl_session_timeout 1d;
NGINX

# 单页应用：入口 index.html 不缓存；带 hash 的 /assets/ 缓存一年；其余路径兜底到入口。
cat > /etc/nginx/snippets/minitv-spa.conf << 'NGINX'
index index.html;
location = /index.html { add_header Cache-Control "no-cache"; }
location /assets/ { add_header Cache-Control "public, max-age=31536000, immutable"; try_files $uri =404; }
location / { try_files $uri $uri/ /index.html; }
NGINX

cat > /etc/nginx/sites-available/minitv-https << NGINX
server {
    server_name $DOMAIN;
    include snippets/minitv-ssl.conf;
    root $WEB_ROOT/web;
    # APK 和 CLI 是普通文件：不存在就返回真实 404，不兜底到 SPA 首页。
    location /download/ { try_files \$uri =404; }
    location /cli/ { default_type text/plain; add_header Cache-Control "no-cache"; try_files \$uri =404; }
    include snippets/minitv-spa.conf;
}

server {
    server_name www.$DOMAIN;
    include snippets/minitv-ssl.conf;
    return 301 https://$DOMAIN\$request_uri;
}

server {
    server_name h5.$DOMAIN;
    include snippets/minitv-ssl.conf;
    root $WEB_ROOT/h5;
    include snippets/minitv-spa.conf;
}

# 后台用 vite base /admin/ 构建：静态资源在 /admin/ 下，BrowserRouter 深链一律交给 /admin/index.html。
server {
    server_name admin.$DOMAIN;
    include snippets/minitv-ssl.conf;
    root $WEB_ROOT/admin-root;
    location = /admin/index.html { add_header Cache-Control "no-cache"; }
    location /admin/assets/ { add_header Cache-Control "public, max-age=31536000, immutable"; try_files \$uri =404; }
    location / { try_files \$uri /admin/index.html; }
}

server {
    server_name api.$DOMAIN;
    include snippets/minitv-ssl.conf;
    client_max_body_size 32m;

    location / {
        proxy_pass http://127.0.0.1:$API_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 60s;
    }
}
NGINX
ln -sfn /etc/nginx/sites-available/minitv-https /etc/nginx/sites-enabled/minitv-https
nginx -t
systemctl reload nginx

# 4.6 健康检查
ok=0
for _ in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:$API_PORT/health" > /dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
if [[ $ok != 1 ]]; then
  echo "API health check failed" >&2
  journalctl -u minitv-api -n 60 --no-pager >&2 || true
  exit 1
fi
echo "api: $(curl -fsS --resolve "api.$DOMAIN:443:127.0.0.1" "https://api.$DOMAIN/health")"
for name in "$DOMAIN" "h5.$DOMAIN" "admin.$DOMAIN"; do
  code=$(curl -sS -o /dev/null -w '%{http_code}' --resolve "$name:443:127.0.0.1" "https://$name/")
  echo "$name: HTTP $code"
  [[ $code == 200 ]] || { echo "站点自检失败：$name" >&2; exit 1; }
done
echo "deploy ok"
REMOTE
