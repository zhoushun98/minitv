"""本机自建媒体后端（``BUNNY_MODE=local``）：磁盘存储 + ffmpeg 转 HLS + Nginx 分发。

``bunny.py`` 的每个网关函数在 local 模式下都转到这里，返回值和 Bunny 的形状一致，
所以上传会话、转码对账、播放签名这些业务代码不需要区分 Bunny 还是本机。

磁盘布局（``MEDIA_ROOT``，默认 ``/srv/minitv-media``）::

    videos/{id}/meta.json     视频对象，字段对齐 Bunny Stream 的 video object
    videos/{id}/source.part   上传中的原片（TUS 追加写入）
    videos/{id}/source        上传完成的原片，转码成功后删除
    hls/{id}/playlist.m3u8    主播放列表；各清晰度在 {h}p/video.m3u8 + seg_NNN.ts
    files/{path}              封面、字幕（对应 Bunny Storage）
    player/                   后台预览用的播放页

Nginx 只暴露 ``hls/``、``files/`` 和 ``player/``：

* ``{MEDIA_BASE_URL}/v/{token}/{expires}/{id}/playlist.m3u8``：``secure_link`` 校验
  目录级 token。分片用相对路径，自动继承同一个 token。
* ``{MEDIA_BASE_URL}/f/{path}``：封面、字幕，公开访问。

token 的算法必须和 Nginx 配置保持一致（见 ``scripts/deploy_lax.sh``）::

    base64url(md5("{expires}:{video_id}:{MEDIA_TOKEN_KEY}"))，去掉末尾的 "="
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

from app.modules.common.bunny import BunnyError

# Bunny Stream 的状态码：0 已创建，1 已上传（排队中），3 转码中，4 完成，5 失败。
STATUS_CREATED = 0
STATUS_UPLOADED = 1
STATUS_TRANSCODING = 3
STATUS_FINISHED = 4
STATUS_FAILED = 5

DEFAULT_ROOT = "/srv/minitv-media"
DEFAULT_RENDITIONS = "360,720"
DEFAULT_MAX_UPLOAD_BYTES = 4 * 1024**3
VIDEO_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise BunnyError(f"BUNNY_MODE=local but {name} is not configured")
    return value


def media_root() -> Path:
    return Path(os.environ.get("MEDIA_ROOT", "").strip() or DEFAULT_ROOT)


def base_url() -> str:
    return _env("MEDIA_BASE_URL").rstrip("/")


def cdn_host() -> str:
    return urlsplit(base_url()).netloc


def token_key() -> str:
    return _env("MEDIA_TOKEN_KEY")


def tus_endpoint() -> str:
    explicit = os.environ.get("MEDIA_TUS_ENDPOINT", "").strip()
    if explicit:
        return explicit
    api = os.environ.get("API_BASE_URL", "").strip().rstrip("/")
    if not api:
        raise BunnyError("BUNNY_MODE=local needs MEDIA_TUS_ENDPOINT or API_BASE_URL")
    return f"{api}/v1/media/tus"


def renditions() -> list[int]:
    """转码目标清晰度（短边像素），升序去重。"""
    raw = os.environ.get("MEDIA_RENDITIONS", "").strip() or DEFAULT_RENDITIONS
    values = sorted({int(item) for item in raw.split(",") if item.strip()})
    if not values or any(value < 144 or value > 2160 for value in values):
        raise BunnyError(f"invalid MEDIA_RENDITIONS: {raw!r}")
    return values


def max_upload_bytes() -> int:
    raw = os.environ.get("MEDIA_MAX_UPLOAD_BYTES", "").strip()
    return int(raw) if raw else DEFAULT_MAX_UPLOAD_BYTES


# ---------------------------------------------------------------------------
# 视频对象（meta.json）
# ---------------------------------------------------------------------------


def check_id(video_id: str) -> str:
    if not VIDEO_ID_RE.fullmatch(video_id or ""):
        raise BunnyError(f"invalid video id: {video_id!r}")
    return video_id


def video_dir(video_id: str) -> Path:
    return media_root() / "videos" / check_id(video_id)


def hls_dir(video_id: str) -> Path:
    return media_root() / "hls" / check_id(video_id)


def part_path(video_id: str) -> Path:
    return video_dir(video_id) / "source.part"


def source_path(video_id: str) -> Path:
    return video_dir(video_id) / "source"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_bytes(data)
    tmp.chmod(mode)
    os.replace(tmp, path)


def read_meta(video_id: str) -> dict | None:
    path = video_dir(video_id) / "meta.json"
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def write_meta(video_id: str, meta: dict) -> dict:
    _atomic_write(video_dir(video_id) / "meta.json", json.dumps(meta, ensure_ascii=False).encode(), 0o600)
    return meta


def update_meta(video_id: str, **changes) -> dict:
    meta = read_meta(video_id)
    if meta is None:
        raise BunnyError(f"get video failed (404: {video_id})")
    meta.update(changes)
    return write_meta(video_id, meta)


def create_video(title: str) -> str:
    video_id = str(uuid.uuid4())
    video_dir(video_id).mkdir(parents=True, exist_ok=False)
    write_meta(
        video_id,
        {
            "guid": video_id,
            "title": title or "untitled",
            "status": STATUS_CREATED,
            "length": 0,
            "width": 0,
            "height": 0,
            "availableResolutions": "",
            "encodeProgress": 0,
            "dateUploaded": now_iso(),
            "hasOriginal": False,
            "storageSize": 0,
            "uploadLength": 0,
            "error": "",
            "local": True,
        },
    )
    return video_id


def get_video(video_id: str) -> dict:
    meta = read_meta(check_id(video_id))
    if meta is None:
        raise BunnyError(f"get video failed (404: {video_id})")
    return meta


def list_videos() -> dict[str, dict]:
    root = media_root() / "videos"
    if not root.is_dir():
        return {}
    out: dict[str, dict] = {}
    for entry in root.iterdir():
        if not VIDEO_ID_RE.fullmatch(entry.name):
            continue
        meta = read_meta(entry.name)
        if meta is not None:
            out[entry.name] = meta
    return out


def delete_video(video_id: str) -> None:
    shutil.rmtree(video_dir(video_id), ignore_errors=True)
    shutil.rmtree(hls_dir(video_id), ignore_errors=True)


# ---------------------------------------------------------------------------
# 上传（TUS 接收端见 app/modules/media/router.py）
# ---------------------------------------------------------------------------


def _tus_signature(video_id: str, expires: int) -> str:
    message = f"tus:{video_id}:{expires}".encode()
    return hmac.new(token_key().encode(), message, hashlib.sha256).hexdigest()


def tus_headers(video_id: str, *, ttl: int = 3600) -> dict[str, str]:
    """和 Bunny 一样的四个头：后台、合作方后台、CLI 原样带在 POST / HEAD / PATCH 上。"""
    expires = int(time.time()) + ttl
    return {
        "AuthorizationSignature": _tus_signature(check_id(video_id), expires),
        "AuthorizationExpire": str(expires),
        "VideoId": video_id,
        "LibraryId": "local",
    }


def verify_tus(video_id: str, signature: str, expires: str) -> bool:
    try:
        check_id(video_id)
        expires_at = int(expires)
    except (BunnyError, ValueError):
        return False
    if expires_at < int(time.time()):
        return False
    return hmac.compare_digest(_tus_signature(video_id, expires_at), signature or "")


def upload_state(video_id: str) -> tuple[int, int, bool]:
    """返回 (已收到字节数, 总长度, 是否已完成)。"""
    meta = get_video(video_id)
    length = int(meta.get("uploadLength") or 0)
    if meta.get("hasOriginal"):
        return length, length, True
    part = part_path(video_id)
    return (part.stat().st_size if part.exists() else 0), length, False


def begin_upload(video_id: str, length: int) -> None:
    meta = get_video(video_id)
    if meta.get("hasOriginal"):
        return
    if length <= 0:
        raise BunnyError("Upload-Length must be positive")
    if length > max_upload_bytes():
        raise BunnyError(f"file too large: {length} > {max_upload_bytes()} bytes")
    part = part_path(video_id)
    if int(meta.get("uploadLength") or 0) != length:
        # 长度变了说明是另一个文件，之前收的字节作废。
        part.unlink(missing_ok=True)
        update_meta(video_id, uploadLength=length)
    part.touch(exist_ok=True)


def finish_upload(video_id: str) -> dict:
    """字节收齐：原片落盘，进入转码队列（status 1）。"""
    os.replace(part_path(video_id), source_path(video_id))
    size = source_path(video_id).stat().st_size
    return update_meta(
        video_id,
        status=STATUS_UPLOADED,
        hasOriginal=True,
        storageSize=size,
        dateQueued=now_iso(),
        error="",
    )


def import_file(video_id: str, file_path: str | os.PathLike[str]) -> None:
    """服务器上已有的文件直接入队（对应 bunny.stream_upload）。"""
    src = Path(file_path)
    size = src.stat().st_size
    get_video(video_id)
    shutil.copyfile(src, part_path(video_id))
    update_meta(video_id, uploadLength=size)
    finish_upload(video_id)


# ---------------------------------------------------------------------------
# 播放签名（Nginx secure_link）
# ---------------------------------------------------------------------------


def playback_token(video_id: str, expires: int) -> str:
    digest = hashlib.md5(f"{expires}:{check_id(video_id)}:{token_key()}".encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def sign_playback(video_id: str, *, ttl: int) -> dict:
    expires = int(time.time()) + ttl
    token = playback_token(video_id, expires)
    url = f"{base_url()}/v/{token}/{expires}/{video_id}/playlist.m3u8"
    return {"token": token, "expires_at": expires, "path": f"/{video_id}/", "url": url, "gen": 0}


def embed_url(video_id: str, *, ttl: int) -> dict:
    """后台预览：自带的 hls.js 播放页，签名后的 m3u8 地址放在 URL hash 里。"""
    signed = sign_playback(video_id, ttl=ttl)
    player = f"{base_url()}/player/#{quote(signed['url'], safe='')}"
    return {"token": signed["token"], "expires_at": signed["expires_at"], "url": player, "embed_url": player}


# ---------------------------------------------------------------------------
# 文件存储（封面、字幕）
# ---------------------------------------------------------------------------


def _storage_rel(remote_path: str) -> PurePosixPath:
    rel = PurePosixPath(str(remote_path).lstrip("/"))
    if not rel.parts or any(part in ("", ".", "..") for part in rel.parts):
        raise BunnyError(f"invalid storage path: {remote_path!r}")
    return rel


def storage_upload(remote_path: str, data: bytes) -> None:
    _atomic_write(media_root() / "files" / _storage_rel(remote_path), data, 0o644)


def storage_delete(remote_path: str) -> None:
    (media_root() / "files" / _storage_rel(remote_path)).unlink(missing_ok=True)


def file_url(remote_path: str) -> str:
    return f"{base_url()}/f/{quote(str(_storage_rel(remote_path)))}"


def sign_file_url(remote_path: str, *, ttl: int) -> dict:
    """封面和字幕公开访问，不带 token；保持和 Bunny 签名结果相同的字段。"""
    parent = str(_storage_rel(remote_path).parent)
    return {
        "token": "",
        "expires_at": int(time.time()) + ttl,
        "path": "/" if parent == "." else f"/{parent}/",
        "url": file_url(remote_path),
        "gen": 0,
    }
