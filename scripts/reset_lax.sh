#!/usr/bin/env bash
# 把 lax 上的 AsiaTV 恢复成「刚部署完的空站」：
#   - 数据库删掉重建（alembic 迁移到最新），重新创建 admin，随机初始密码只打印一次；
#   - 删除全部原片、HLS 和封面等媒体文件（$MEDIA_ROOT/player 播放页保留）；
#   - 清空 Redis。
# 保留：服务器软件和配置、域名与证书、$REMOTE_DIR/.env 里的全部密钥、官网 / H5 / 后台 / APK。
#
# 操作不可撤销：必须把 MINITV_RESET_CONFIRM 设成域名才会执行，并且只能经发布脚本调用（持锁、留记录）：
#   MINITV_RESET_CONFIRM=sanaoman.asia python3 scripts/release.py run --target lax-reset \
#     --note "清空全部数据" -- ./scripts/reset_lax.sh
set -euo pipefail

HOST="${MINITV_DEPLOY_HOST:-lax}"
DOMAIN="${MINITV_DOMAIN:-sanaoman.asia}"
REMOTE_DIR="${MINITV_REMOTE_DIR:-/opt/minitv}"
API_PORT="${MINITV_API_PORT:-18110}"
MEDIA_ROOT="${MINITV_MEDIA_ROOT:-/srv/minitv-media}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=15 -o ServerAliveCountMax=3)

if [[ "${MINITV_RESET_CONFIRM:-}" != "$DOMAIN" ]]; then
  echo "这会永久删除 $DOMAIN 的全部数据（剧、分集、视频、用户、订单……），无法恢复。" >&2
  echo "确认要执行，请设置 MINITV_RESET_CONFIRM=$DOMAIN，再经 release.py 重跑。" >&2
  exit 2
fi

echo "→ reset $HOST ($DOMAIN) to an empty site"
ssh "${SSH_OPTS[@]}" "$HOST" \
  "DOMAIN='$DOMAIN' REMOTE_DIR='$REMOTE_DIR' API_PORT='$API_PORT' MEDIA_ROOT='$MEDIA_ROOT' bash -s" << 'REMOTE'
set -euo pipefail
cd /tmp

# 防止连错机器或删错目录：.env 必须属于这个域名，媒体目录必须在 /srv 下。
grep -qF "api.$DOMAIN" "$REMOTE_DIR/.env" || { echo "$REMOTE_DIR/.env 不属于 $DOMAIN，拒绝执行" >&2; exit 1; }
case "$MEDIA_ROOT" in
  /srv/?*) ;;
  *) echo "MEDIA_ROOT=$MEDIA_ROOT 不在 /srv 下，拒绝执行" >&2; exit 1 ;;
esac
set -a
# shellcheck disable=SC1091
source "$REMOTE_DIR/.env"
set +a

echo "→ stop api / media worker"
systemctl stop minitv-api minitv-media

echo "→ recreate database"
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -q << 'SQL'
DROP DATABASE IF EXISTS minitv WITH (FORCE);
CREATE DATABASE minitv OWNER minitv;
GRANT ALL PRIVILEGES ON DATABASE minitv TO minitv;
SQL
runuser -u postgres -- psql -d minitv -v ON_ERROR_STOP=1 -q -c "GRANT ALL ON SCHEMA public TO minitv;"

echo "→ wipe media (player/ kept)"
find "$MEDIA_ROOT/videos" "$MEDIA_ROOT/hls" "$MEDIA_ROOT/files" -mindepth 1 -delete

echo "→ flush redis"
[[ $(REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli -h 127.0.0.1 FLUSHALL) == OK ]] || { echo "redis FLUSHALL 失败" >&2; exit 1; }

echo "→ migrate + create admin"
cd "$REMOTE_DIR/backend"
../.venv/bin/alembic upgrade head
seed_out=$(../.venv/bin/python -m app.seed_admin)
echo "$seed_out"
username=$(sed -n 's/^created: \([^ ]*\) .*/\1/p' <<< "$seed_out")
password=$(sed -n 's/^initial password (shown once): //p' <<< "$seed_out")
cd /tmp

echo "→ start api / media worker"
systemctl start minitv-api minitv-media
healthy=0
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$API_PORT/health" > /dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 1
done
if [[ $healthy != 1 ]]; then
  echo "API 启动后健康检查失败" >&2
  journalctl -u minitv-api -n 60 --no-pager >&2 || true
  exit 1
fi
curl -fsS --max-time 10 --resolve "api.$DOMAIN:443:127.0.0.1" "https://api.$DOMAIN/health" > /dev/null
systemctl is-active --quiet minitv-media

echo "→ verify"
counts=$(runuser -u postgres -- psql -d minitv -At -c \
  "select (select count(*) from dramas), (select count(*) from episodes), (select count(*) from users), (select count(*) from admin_users)")
echo "dramas|episodes|users|admin_users = $counts"
[[ $counts == "0|0|0|1" ]] || { echo "数据没有清干净" >&2; exit 1; }
left=$(find "$MEDIA_ROOT/videos" "$MEDIA_ROOT/hls" "$MEDIA_ROOT/files" -mindepth 1 | wc -l)
[[ $left == 0 ]] || { echo "媒体目录还剩 $left 个文件" >&2; exit 1; }
if [[ -n $password ]]; then
  # 密码经 stdin 传给 curl，不出现在进程列表里。
  code=$(printf '{"username":"%s","password":"%s"}' "$username" "$password" \
    | curl -sS -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' --data @- \
      "http://127.0.0.1:$API_PORT/admin/v1/auth/login")
  [[ $code == 200 ]] || { echo "新 admin 密码登录校验失败（HTTP $code）" >&2; exit 1; }
  echo "admin login ok"
else
  echo "（.env 设置了 SEED_ADMIN_PASSWORD，admin 密码沿用该值）"
fi
echo "reset done"
REMOTE
