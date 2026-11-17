# lax / sanaoman.asia 全栈部署

API、官网、H5、管理后台全部跑在服务器 `lax` 上（SSH 别名，见本机 `~/.ssh/config`）。Cloudflare 只负责 DNS 解析，记录全部是灰云（不经过代理）。HTTPS 证书由服务器上的 Let's Encrypt 签发，并自动续期。

## 域名布局

| 域名 | 内容 | 服务器路径 |
| --- | --- | --- |
| `sanaoman.asia` | 官网 | `/var/www/minitv/web` |
| `www.sanaoman.asia` | 301 → `sanaoman.asia` | — |
| `h5.sanaoman.asia` | H5 | `/var/www/minitv/h5` |
| `admin.sanaoman.asia` | 管理后台（vite base `/admin/`，深链兜底到 `/admin/index.html`） | `/var/www/minitv/admin-root/admin` |
| `api.sanaoman.asia` | FastAPI，Nginx 反代到 `127.0.0.1:18110` | `/opt/minitv` |

DNS 上是 5 条 A 记录（`@ / www / h5 / admin / api`），全部 `proxied=false`。服务器禁用了 IPv6，所以没有 AAAA 记录，Nginx 也只监听 IPv4。

另外两个静态目录：

- `https://sanaoman.asia/cli/`：`cli/install.sh` 和 `cli/minitv`，部署时同步，也就是后台「API Key」页面里给出的安装命令所用的文件。
- `https://sanaoman.asia/download/`：手工放 APK（`/var/www/minitv/web/download/`），部署不会删。文件名必须和 `web/src/downloads.ts` 一致；文件不存在时返回真实的 404。

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

## 补第三方密钥

首次部署是空库，视频服务为 `BUNNY_MODE=fake`。以下功能要等对应密钥配好后才能用：

| 服务 | 功能 | `.env` 键 |
| --- | --- | --- |
| Bunny | 视频上传、转码、播放，封面 CDN | `BUNNY_MODE=real` 加上全部 `BUNNY_*`（说明见 `.env.example`） |
| Resend | 注册 / 登录验证码邮件 | `RESEND_API_KEY`、`RESEND_FROM` |
| Stripe | 会员订阅、购买金币 | `STRIPE_SECRET_KEY`、`STRIPE_WEBHOOK_SECRET` |

操作：`ssh lax`，编辑 `/opt/minitv/.env`（模板里已有注释占位），然后执行 `systemctl restart minitv-api`。不需要重新部署。

## 运维速查

```bash
ssh lax systemctl status minitv-api nginx postgresql redis-server
ssh lax journalctl -u minitv-api -n 100 --no-pager
ssh lax certbot certificates          # 证书有效期；certbot.timer 负责自动续期
```
