# lax / sanaoman.asia 全栈部署

API、官网、H5、管理后台全部跑在服务器 `lax` 上（SSH 别名，见本机 `~/.ssh/config`）。Cloudflare 只负责 DNS 解析，记录全部是灰云（不经过代理）。HTTPS 证书由服务器上的 Let's Encrypt 签发，并自动续期。

## 域名布局

| 域名 | 内容 | 服务器路径 |
| --- | --- | --- |
| `sanaoman.asia` | 官网 | `/var/www/minitv/web` |
| `www.sanaoman.asia` | 301 → `sanaoman.asia` | — |
| `h5.sanaoman.asia` | H5 | `/var/www/minitv/h5` |
| `admin.sanaoman.asia` | 管理后台（vite base `/admin/`，深链兜底到 `/admin/index.html`） | `/var/www/minitv/admin-root/admin` |
| `api.sanaoman.asia` | FastAPI，Nginx 反代到 `127.0.0.1:18110`；`/v1/media/tus` 是视频直传入口 | `/opt/minitv` |
| `media.sanaoman.asia` | 视频（HLS）、封面、后台预览播放页 | `/srv/minitv-media` |

DNS 上是 6 条 A 记录（`@ / www / h5 / admin / api / media`），全部 `proxied=false`。服务器禁用了 IPv6，所以没有 AAAA 记录，Nginx 也只监听 IPv4。

另外两个静态目录：

- `https://sanaoman.asia/cli/`：`cli/install.sh` 和 `cli/minitv`，部署时同步，也就是后台「API Key」页面里给出的安装命令所用的文件。
- `https://sanaoman.asia/download/`：Android APK（服务器路径 `/var/www/minitv/web/download/`），由 `scripts/publish_apk_lax.sh` 发布，部署不会删除。文件不存在时返回真实的 404。

## 发布

```bash
python3 scripts/release.py status
python3 scripts/release.py run --target lax --note "说明" -- ./scripts/deploy_lax.sh
python3 scripts/release.py status
```

`scripts/deploy_lax.sh` 是幂等的，可以重复执行。它依次完成：

1. 在本地构建三个前端（`VITE_API_BASE=https://api.$DOMAIN`），并自检产物里的域名。
2. 在服务器上安装 nginx / postgresql / redis-server / python3-venv / certbot。
3. rsync `backend/`、三个 `dist/` 和 `cli/`。
4. 首次部署时在服务器上生成 `/opt/minitv/.env`（随机生成数据库口令和各类签名密钥）；已存在的 `.env` 不会被覆盖。
5. 依次执行 `alembic upgrade head`、`app.seed_admin`（只在没有后台账号时创建 `admin`，并把随机初始密码**打印一次**），然后重启 systemd 服务 `minitv-api`。
6. 配置 Nginx 80 端口（ACME 验证 + 跳转 https），用 certbot webroot 申请或续用证书，再写 443 配置。
7. 健康检查：本机 `/health`，加上通过 HTTPS 访问 4 个站点。

可用环境变量覆盖：`MINITV_DEPLOY_HOST`（默认 `lax`）、`MINITV_DOMAIN`（默认 `sanaoman.asia`）、`MINITV_REMOTE_DIR`、`MINITV_WEB_ROOT`、`MINITV_API_PORT`。

## 视频：本机自建（`BUNNY_MODE=local`）

视频不经过任何第三方，整条链路都在 lax 上：

1. **上传**：后台、合作方后台、CLI 用 TUS 直传到 `https://api.sanaoman.asia/v1/media/tus`。协议和授权头与 Bunny 相同，所以客户端不用改；支持断点续传。单个视频上限 2GB，由后端 `upload_service.MAX_BYTES` 决定（后台和 CLI 有同值常量，要一起改）；接收端的硬上限是 4 GiB。上传超过一小时没再收到字节才会被判为卡死，慢链路传大文件不会被误杀。
2. **转码**：systemd 服务 `minitv-media`（`backend/app/media_worker.py`）一次转一集，按 `MEDIA_RENDITIONS`（默认 360p、720p，不放大）转成 HLS，以低优先级运行。转完立即把分集从「转码中」推进到「审核中」，并删除原片。
3. **播放**：API 签发 `https://media.sanaoman.asia/v/{token}/{expires}/{id}/playlist.m3u8`，5 分钟有效，由 Nginx `secure_link` 校验：伪造返回 403，过期返回 410。分片用相对路径，继承同一个 token。
4. **封面和字幕**：存在 `/srv/minitv-media/files/`，通过 `https://media.sanaoman.asia/f/...` 公开访问。
5. **清理**：worker 每小时删除一次没有被任何分集引用、且创建超过一天的视频（被替换或删除的旧视频）。

磁盘布局与 token 算法见 `backend/app/modules/common/local_media.py`。代码对 Bunny 的调用都经过 `bunny.py` 网关，以后要换回 Bunny，只需把 `BUNNY_MODE` 改成 `real` 并填好 `BUNNY_*`。

运维：

```bash
ssh lax systemctl status minitv-media
ssh lax journalctl -u minitv-media -n 50 --no-pager   # 每集的转码耗时、失败原因
ssh lax du -sh /srv/minitv-media/hls                   # 视频占用的磁盘
```

## 补第三方密钥

以下功能要等对应密钥配好后才能用：

| 服务 | 功能 | `.env` 键 |
| --- | --- | --- |
| Resend | 注册 / 登录验证码邮件（已配置） | `RESEND_API_KEY`、`RESEND_FROM` |
| Stripe | 会员订阅、购买金币 | `STRIPE_SECRET_KEY`、`STRIPE_WEBHOOK_SECRET` |

用 `scripts/lax_env.sh` 写入：参数只写变量名，值从当前 shell 环境读取，经 ssh stdin 传给服务器，所以密钥不会进入命令行、发布记录或 git。脚本会替换 `.env` 里的同名行或注释占位，修改前的版本备份为 `.env.bak`，然后重启 `minitv-api` 和 `minitv-media` 并做健康检查。

```bash
RESEND_API_KEY=re_xxx RESEND_FROM='AsiaTV <noreply@sanaoman.asia>' \
  python3 scripts/release.py run --target lax-env --note "配置 Resend" -- \
  ./scripts/lax_env.sh RESEND_API_KEY RESEND_FROM
```

不需要重新部署代码。

## Android APK

```bash
python3 scripts/release.py run --target lax-apk --note "APK 1.0.0" -- ./scripts/publish_apk_lax.sh
```

- 在本地用 Flutter 构建 arm64 release 包，`--dart-define=API_BASE=https://api.sanaoman.asia`；构建完会自检 `libapp.so` 里确实有这个 API 地址，然后上传到 `/download/`，并核对线上的状态码和文件大小；校验通过后删除 `/download/` 里其余的旧 APK（官网只链接当前这一个）。
- 文件名和版本以 `web/src/downloads.ts` 为准，而且必须与 `app/pubspec.yaml` 的 `version` 一致。发新版时 downloads.ts（url / version / size）和 pubspec 一起改，然后先跑 `deploy_lax.sh` 更新官网按钮，再跑本脚本。
- 签名沿用 `app/android` 的配置（release 用本机 `~/.android/debug.keystore`）。必须在同一台机器上构建，否则用户装了旧版就无法覆盖升级。
- 依赖：Flutter ≥ 3.47（`brew install --cask flutter`）、JDK 17、Android SDK（默认 `/opt/homebrew/share/android-commandlinetools`）。

## 运维速查

```bash
ssh lax systemctl status minitv-api minitv-media nginx postgresql redis-server
ssh lax journalctl -u minitv-api -n 100 --no-pager
ssh lax certbot certificates          # 证书有效期；certbot.timer 负责自动续期
```

### 清空全部数据（恢复成空站）

不可撤销。会删除数据库、全部视频和封面、Redis 数据，然后重新创建 admin 并打印一次新密码；密钥、证书、站点和 APK 保持不变。

```bash
MINITV_RESET_CONFIRM=sanaoman.asia python3 scripts/release.py run --target lax-reset \
  --note "清空全部数据" -- ./scripts/reset_lax.sh
```
