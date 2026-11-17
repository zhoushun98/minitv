#!/usr/bin/env bash
# 把本地 shell 环境里的指定变量写进服务器的 $REMOTE_DIR/.env，然后重启 minitv-api / minitv-media 并做健康检查。
#
# 参数只写变量名，值从当前环境读取，经 ssh stdin 传给服务器。所以密钥不会出现在
# 命令行参数、发布记录（deployments/releases/*.json 只记命令）或 git 里。
#
# 用法（经发布脚本：持锁、留记录）：
#   RESEND_API_KEY=re_xxx RESEND_FROM='AsiaTV <noreply@sanaoman.asia>' \
#     python3 scripts/release.py run --target lax-env --note "配置 Resend" -- \
#     ./scripts/lax_env.sh RESEND_API_KEY RESEND_FROM
#
# 已有的同名行（包括模板里的 "# KEY=" 注释占位）会被原地替换，修改前的版本备份为 .env.bak。
set -euo pipefail

HOST="${MINITV_DEPLOY_HOST:-lax}"
REMOTE_DIR="${MINITV_REMOTE_DIR:-/opt/minitv}"
API_PORT="${MINITV_API_PORT:-18110}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=15 -o ServerAliveCountMax=3)
remote() { ssh "${SSH_OPTS[@]}" "$HOST" "$@"; }

if [[ $# -eq 0 ]]; then
  echo "用法: $0 KEY [KEY ...]（值从当前环境变量读取）" >&2
  exit 2
fi

# 校验并打包成 JSON。值只能是一行且不含单引号：服务器 .env 会被 systemd 读取，也会被部署脚本 source。
payload="$(python3 - "$@" << 'PY'
import json
import os
import re
import sys

out = {}
for name in sys.argv[1:]:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
        sys.exit(f"非法变量名: {name}")
    value = os.environ.get(name, "")
    if not value.strip():
        sys.exit(f"环境变量 {name} 为空，拒绝写入")
    if "\n" in value or "'" in value:
        sys.exit(f"{name} 的值不能包含换行或单引号")
    out[name] = value
print(json.dumps(out))
PY
)"

echo "→ update $HOST:$REMOTE_DIR/.env: $*"
printf '%s' "$payload" | remote "umask 077; cat > /tmp/minitv-env-update.json"

remote "REMOTE_DIR='$REMOTE_DIR' API_PORT='$API_PORT' bash -s" << 'REMOTE'
set -euo pipefail
trap 'rm -f /tmp/minitv-env-update.json' EXIT
python3 - << 'PY'
import json
import os
import re
import shutil
from pathlib import Path

env = Path(os.environ["REMOTE_DIR"]) / ".env"
updates = json.loads(Path("/tmp/minitv-env-update.json").read_text())
lines = env.read_text().splitlines()
safe = re.compile(r"[A-Za-z0-9_@.:/+=,-]*")

for key, value in updates.items():
    rendered = f"{key}={value}" if safe.fullmatch(value) else f"{key}='{value}'"
    pattern = re.compile(rf"^\s*#?\s*{re.escape(key)}=")
    hits = [i for i, line in enumerate(lines) if pattern.match(line)]
    if hits:
        lines[hits[0]] = rendered
        for i in reversed(hits[1:]):
            del lines[i]
    else:
        lines.append(rendered)
    print(f"  set {key}")

shutil.copy2(env, env.with_name(".env.bak"))
tmp = env.with_name(".env.tmp")
tmp.write_text("\n".join(lines) + "\n")
tmp.chmod(0o600)
os.replace(tmp, env)
PY

systemctl restart minitv-api
# 转码 worker 也读同一份 .env（比如 BUNNY_MODE / MEDIA_*）；没装这个服务时 try-restart 什么都不做。
systemctl try-restart minitv-media
for _ in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:$API_PORT/health" > /dev/null 2>&1; then
    echo "api restarted, health ok"
    exit 0
  fi
  sleep 1
done
echo "API health check failed after env update（修改前的版本在 .env.bak）" >&2
journalctl -u minitv-api -n 60 --no-pager >&2 || true
exit 1
REMOTE
