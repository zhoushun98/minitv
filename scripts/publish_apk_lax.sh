#!/usr/bin/env bash
# 构建 Android APK（arm64-v8a，API 指向 https://api.$DOMAIN），发布到 https://$DOMAIN/download/。
#
# 用法（经发布脚本：持锁、留记录）：
#   python3 scripts/release.py run --target lax-apk --note "APK x.y.z" -- ./scripts/publish_apk_lax.sh
#
# 文件名和版本以 web/src/downloads.ts 为准（官网按钮直接指向它；download/ 里只保留这一个 APK），
# 并且必须与 app/pubspec.yaml 的 version 一致；发新版时三处一起改。签名沿用 app/android 的配置
# （release 用 debug key），所以要在同一台机器上构建，否则旧版无法覆盖升级。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${MINITV_DEPLOY_HOST:-lax}"
DOMAIN="${MINITV_DOMAIN:-sanaoman.asia}"
WEB_ROOT="${MINITV_WEB_ROOT:-/var/www/minitv}"
FLUTTER="${FLUTTER:-flutter}"
export ANDROID_HOME="${ANDROID_HOME:-/opt/homebrew/share/android-commandlinetools}"
export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-$ANDROID_HOME}"
RSYNC_SSH="ssh -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=15 -o ServerAliveCountMax=3"

# downloads.ts 的 url / version 和 pubspec 的 version 必须对得上。
read -r APK_PATH APK_VERSION < <(python3 - "$ROOT" << 'PY'
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
ts = (root / "web/src/downloads.ts").read_text()
url = re.search(r'url:\s*"([^"]+)"', ts).group(1)
version = re.search(r'version:\s*"([^"]+)"', ts).group(1)
pub = re.search(r"^version:\s*([0-9.]+)", (root / "app/pubspec.yaml").read_text(), re.M).group(1)
if not url.startswith("/download/") or "/" in url[len("/download/"):]:
    sys.exit(f"downloads.ts 的 url 必须是 /download/<文件名>，实际是 {url}")
if version != pub:
    sys.exit(f"版本不一致：downloads.ts={version}，pubspec.yaml={pub}")
print(url, version)
PY
)
[[ -n "${APK_PATH:-}" ]] || { echo "读取 downloads.ts / pubspec.yaml 失败" >&2; exit 1; }
APK_NAME="${APK_PATH#/download/}"
echo "→ build $APK_NAME (v$APK_VERSION, API https://api.$DOMAIN)"

(
  cd "$ROOT/app"
  "$FLUTTER" pub get
  "$FLUTTER" build apk --release --target-platform android-arm64 \
    --dart-define=API_BASE="https://api.$DOMAIN"
)
APK="$ROOT/app/build/app/outputs/flutter-apk/app-release.apk"
[[ -f "$APK" ]] || { echo "找不到构建产物 $APK" >&2; exit 1; }

# AOT 快照里能直接搜到字符串常量：确认 API 地址打进去了。
# 用 grep -c 读完整个流；grep -q 会提前退出，让 unzip 收到 SIGPIPE，在 pipefail 下被误判为失败。
hits=$(unzip -p "$APK" lib/arm64-v8a/libapp.so | LC_ALL=C grep -ac "api\.$DOMAIN" || true)
if [[ ${hits:-0} -eq 0 ]]; then
  echo "APK 自检失败：libapp.so 里找不到 api.$DOMAIN" >&2
  exit 1
fi
SIZE=$(wc -c < "$APK" | tr -d ' ')
echo "  apk ok: $((SIZE / 1024 / 1024)) MB ($SIZE bytes)"

echo "→ upload to $HOST:$WEB_ROOT/web/download/$APK_NAME"
rsync -az -e "$RSYNC_SSH" "$APK" "$HOST:$WEB_ROOT/web/download/$APK_NAME"
ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" "chmod 644 '$WEB_ROOT/web/download/$APK_NAME'"

read -r code ctype length < <(curl -sS -o /dev/null -I --connect-timeout 8 --max-time 60 \
  -w '%{http_code} %{content_type} %header{content-length}\n' "https://$DOMAIN$APK_PATH")
echo "  https://$DOMAIN$APK_PATH → $code $ctype $length bytes"
if [[ $code != 200 || $length != "$SIZE" ]]; then
  echo "线上校验失败（期望 200 / $SIZE bytes）" >&2
  exit 1
fi
# 官网只链接当前这一个 APK：新包校验通过后，再删掉 download/ 里其余的旧 APK。
ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" \
  "find '$WEB_ROOT/web/download' -maxdepth 1 -type f -name '*.apk' ! -name '$APK_NAME' -print -delete" |
  sed 's/^/  removed old apk: /'
echo "apk published"
