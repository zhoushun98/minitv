"""BUNNY_MODE=local：本机自建媒体（TUS 上传 → ffmpeg 转码 → secure_link 播放 → 封面存储）。"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

import pytest
from fastapi.testclient import TestClient

from app import media_worker
from app.main import app
from app.modules.common import bunny, local_media
from app.modules.content.models import Episode
from app.modules.ops.flow_router import _remote_upload_stalled

KEY = "test-media-token-key"
MEDIA = "https://media.example.test"
TUS = "https://api.example.test/v1/media/tus"
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture()
def media_root(monkeypatch, tmp_path):
    root = tmp_path / "media"
    monkeypatch.setenv("BUNNY_MODE", "local")
    monkeypatch.setenv("MEDIA_ROOT", str(root))
    monkeypatch.setenv("MEDIA_BASE_URL", MEDIA)
    monkeypatch.setenv("MEDIA_TOKEN_KEY", KEY)
    monkeypatch.setenv("MEDIA_TUS_ENDPOINT", TUS)
    monkeypatch.setenv("MEDIA_RENDITIONS", "360,720")
    return root


@pytest.fixture()
def client(session):
    return TestClient(app)


def _login(client, username, password="pass-1234"):
    response = client.post("/admin/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _sample_video(path, size="360x640", seconds=3):
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
            "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
            str(path),
        ],
        check=True,
    )
    return path


def _tus_upload(client, upload_url_path, headers, data, *, chunks=1):
    created = client.post(upload_url_path, headers={**headers, "Upload-Length": str(len(data))})
    assert created.status_code == 201, created.text
    location = created.headers["Location"]
    step = -(-len(data) // chunks)
    offset = 0
    while offset < len(data):
        piece = data[offset : offset + step]
        patched = client.patch(
            location,
            content=piece,
            headers={**headers, "Upload-Offset": str(offset), "Content-Type": "application/offset+octet-stream"},
        )
        assert patched.status_code == 204, patched.text
        offset += len(piece)
        assert patched.headers["Upload-Offset"] == str(offset)
    return location


# ---------------------------------------------------------------------------
# 网关
# ---------------------------------------------------------------------------


def test_local_mode_is_live_and_uses_own_tus_endpoint(media_root):
    assert bunny.mode() == "local"
    assert bunny.live() and bunny.local()
    assert bunny.tus_endpoint() == TUS
    assert bunny.stream_cdn_host() == "media.example.test"


def test_playback_url_matches_nginx_secure_link(media_root):
    video_id = bunny.create_stream_video("ep1.mp4")
    signed = bunny.sign_stream_playback(video_id, ttl=300)
    expires = signed["expires_at"]
    digest = hashlib.md5(f"{expires}:{video_id}:{KEY}".encode()).digest()
    token = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    assert signed["url"] == f"{MEDIA}/v/{token}/{expires}/{video_id}/playlist.m3u8"
    assert 0 < expires - datetime.now(timezone.utc).timestamp() <= 301


def test_admin_preview_is_self_hosted_player(media_root):
    video_id = bunny.create_stream_video("ep1.mp4")
    preview = bunny.stream_embed_url(video_id, ttl=3600)
    assert preview["embed_url"].startswith(f"{MEDIA}/player/#")
    inner = unquote(urlsplit(preview["embed_url"]).fragment)
    assert inner.startswith(f"{MEDIA}/v/") and inner.endswith(f"/{video_id}/playlist.m3u8")


def test_storage_files_are_public_urls_and_reject_traversal(media_root):
    bunny.storage_upload("covers/7.jpg", b"jpeg-bytes", "image/jpeg")
    assert (media_root / "files" / "covers" / "7.jpg").read_bytes() == b"jpeg-bytes"
    assert bunny.image_url("covers/7.jpg") == f"{MEDIA}/f/covers/7.jpg"
    assert bunny.sign_image_url("subs/3/en.vtt", ttl=300)["url"] == f"{MEDIA}/f/subs/3/en.vtt"
    bunny.storage_delete("covers/7.jpg")
    assert not (media_root / "files" / "covers" / "7.jpg").exists()
    for bad in ("../etc/passwd", "covers/../../x", ""):
        with pytest.raises(bunny.BunnyError):
            bunny.storage_upload(bad, b"x")


def test_local_queue_never_counts_as_stalled():
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    assert _remote_upload_stalled({"local": True, "status": 1, "dateUploaded": old}) is False
    # 建了会话但字节一直没到：和 Bunny 一样判为停滞，分集才能重新上传。
    assert _remote_upload_stalled({"local": True, "status": 0, "dateUploaded": old}) is True


def test_long_upload_is_stalled_only_after_bytes_stop(media_root):
    video_id = bunny.create_stream_video("big.mp4")
    local_media.update_meta(video_id, dateUploaded=(datetime.now(timezone.utc) - timedelta(hours=3)).isoformat())
    local_media.begin_upload(video_id, 10_000)
    with local_media.part_path(video_id).open("ab") as fh:
        fh.write(b"x" * 4096)

    # 会话 3 小时前创建，但刚刚还在收字节：大文件慢链路，不能判为停滞。
    live = bunny.stream_video(video_id)
    assert live["uploadedBytes"] == 4096 and not live["hasOriginal"] and live["storageSize"] == 0
    assert _remote_upload_stalled(live) is False
    assert _remote_upload_stalled(local_media.list_videos()[video_id]) is False

    # 超过一小时没再收到字节：断掉的上传，判为停滞，分集才能重新上传。
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()
    os.utime(local_media.part_path(video_id), (stale, stale))
    assert _remote_upload_stalled(bunny.stream_video(video_id)) is True
    # 实时进度只出现在返回值里，不写回 meta.json。
    assert "uploadedBytes" not in local_media.read_meta(video_id)


def test_upload_limit_is_2gb(media_root, client, make):
    headers = _login(client, make.admin(role="ops").username)
    episode = make.episode(make.drama(), 1)

    def open_session(size):
        return client.post(
            f"/admin/v1/episodes/{episode.id}/upload-session",
            json={"filename": "ep1.mp4", "mime": "video/mp4", "size_bytes": size, "width": 1080, "height": 1920},
            headers=headers,
        )

    too_big = open_session(2 * 1024**3 + 1)
    assert too_big.status_code == 422 and "2GB" in too_big.text
    assert open_session(1500 * 1024**2).status_code == 200


# ---------------------------------------------------------------------------
# TUS 接收端
# ---------------------------------------------------------------------------


def test_tus_upload_resumes_and_queues_the_video(media_root, client):
    video_id = bunny.create_stream_video("ep1.mp4")
    headers = bunny.tus_headers(video_id)
    data = bytes(range(256)) * 400
    created = client.post("/v1/media/tus", headers={**headers, "Upload-Length": str(len(data))})
    assert created.status_code == 201
    location = created.headers["Location"]
    assert location == f"/v1/media/tus/{video_id}"

    half = len(data) // 2
    first = client.patch(location, content=data[:half], headers={**headers, "Upload-Offset": "0"})
    assert first.status_code == 204 and first.headers["Upload-Offset"] == str(half)
    head = client.head(location, headers=headers)
    assert head.headers["Upload-Offset"] == str(half) and head.headers["Upload-Length"] == str(len(data))
    assert bunny.stream_video(video_id)["status"] == local_media.STATUS_CREATED

    wrong = client.patch(location, content=data[half:], headers={**headers, "Upload-Offset": "5"})
    assert wrong.status_code == 409
    rest = client.patch(location, content=data[half:], headers={**headers, "Upload-Offset": str(half)})
    assert rest.status_code == 204 and rest.headers["Upload-Offset"] == str(len(data))

    meta = bunny.stream_video(video_id)
    assert meta["status"] == local_media.STATUS_UPLOADED and meta["hasOriginal"] is True
    assert local_media.source_path(video_id).read_bytes() == data
    # 已完成的上传再 PATCH 是幂等的。
    again = client.patch(location, content=b"", headers={**headers, "Upload-Offset": str(len(data))})
    assert again.status_code == 204


def test_tus_patch_on_creation_url_uses_video_id_header(media_root, client):
    video_id = bunny.create_stream_video("ep1.mp4")
    headers = bunny.tus_headers(video_id)
    data = b"x" * 1000
    assert client.post("/v1/media/tus", headers={**headers, "Upload-Length": "1000"}).status_code == 201
    patched = client.patch("/v1/media/tus", content=data, headers={**headers, "Upload-Offset": "0"})
    assert patched.status_code == 204
    assert bunny.stream_video(video_id)["status"] == local_media.STATUS_UPLOADED


def test_tus_restart_from_zero_discards_partial_bytes(media_root, client):
    video_id = bunny.create_stream_video("ep1.mp4")
    headers = bunny.tus_headers(video_id)
    data = b"abcdefghij" * 100
    location = client.post("/v1/media/tus", headers={**headers, "Upload-Length": str(len(data))}).headers["Location"]
    client.patch(location, content=b"zzzz", headers={**headers, "Upload-Offset": "0"})
    redo = client.patch(location, content=data, headers={**headers, "Upload-Offset": "0"})
    assert redo.status_code == 204
    assert local_media.source_path(video_id).read_bytes() == data


def test_tus_rejects_bad_signature_expired_and_oversize(media_root, client, monkeypatch):
    video_id = bunny.create_stream_video("ep1.mp4")
    good = bunny.tus_headers(video_id)
    forged = {**good, "AuthorizationSignature": "0" * 64}
    assert client.post("/v1/media/tus", headers={**forged, "Upload-Length": "10"}).status_code == 403
    expired = bunny.tus_headers(video_id, ttl=-5)
    assert client.post("/v1/media/tus", headers={**expired, "Upload-Length": "10"}).status_code == 403
    other = bunny.create_stream_video("ep2.mp4")
    swapped = {**good, "VideoId": other}
    assert client.post("/v1/media/tus", headers={**swapped, "Upload-Length": "10"}).status_code == 403

    location = client.post("/v1/media/tus", headers={**good, "Upload-Length": "10"}).headers["Location"]
    too_long = client.patch(location, content=b"x" * 11, headers={**good, "Upload-Offset": "0"})
    assert too_long.status_code == 413
    monkeypatch.setenv("MEDIA_MAX_UPLOAD_BYTES", "100")
    assert client.post("/v1/media/tus", headers={**good, "Upload-Length": "101"}).status_code == 413

    monkeypatch.setenv("BUNNY_MODE", "fake")
    assert client.post("/v1/media/tus", headers={**good, "Upload-Length": "10"}).status_code == 404


def test_tus_cors_exposes_location_to_admin_origin(media_root, client):
    video_id = bunny.create_stream_video("ep1.mp4")
    headers = {**bunny.tus_headers(video_id), "Upload-Length": "10", "Origin": "https://admin.sanaoman.asia"}
    created = client.post("/v1/media/tus", headers=headers)
    assert created.status_code == 201
    exposed = created.headers.get("access-control-expose-headers", "")
    assert "Location" in exposed and "Upload-Offset" in exposed


# ---------------------------------------------------------------------------
# 端到端：后台建会话 → 上传 → 转码 → 分集进入审核中
# ---------------------------------------------------------------------------


@needs_ffmpeg
def test_admin_upload_transcodes_and_reaches_review(media_root, client, make, session, tmp_path):
    headers = _login(client, make.admin(role="ops").username)
    drama = make.drama(title="Local Drama")
    episode = make.episode(drama, 1)
    video = _sample_video(tmp_path / "ep1.mp4").read_bytes()

    opened = client.post(
        f"/admin/v1/episodes/{episode.id}/upload-session",
        json={"filename": "ep1.mp4", "mime": "video/mp4", "size_bytes": len(video), "width": 360, "height": 640},
        headers=headers,
    )
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["upload_url"] == TUS and body["headers"]["VideoId"] == body["video_id"]

    _tus_upload(client, "/v1/media/tus", body["headers"], video, chunks=3)
    synced = client.post(f"/admin/v1/episodes/{episode.id}/sync-status", headers=headers)
    assert synced.json()["status"] == "transcoding"  # 已上传，排队等 worker

    assert media_worker.next_job() == body["video_id"]
    meta = media_worker.process(body["video_id"])
    assert meta["status"] == local_media.STATUS_FINISHED, meta.get("error")
    assert meta["availableResolutions"] == "360p" and meta["length"] == 3

    session.expire_all()
    refreshed = session.get(Episode, episode.id)
    assert refreshed.status == "in_review"  # worker 转完立即对账
    assert refreshed.duration_seconds == 3
    hls = local_media.hls_dir(body["video_id"])
    assert "360p/video.m3u8" in (hls / "playlist.m3u8").read_text()
    assert list((hls / "360p").glob("seg_*.ts"))
    assert not local_media.source_path(body["video_id"]).exists()

    preview = client.get(f"/admin/v1/episodes/{episode.id}/preview-url", headers=_login(client, make.admin(role="admin").username))
    assert preview.status_code == 200 and preview.json()["embed_url"].startswith(f"{MEDIA}/player/#")


@needs_ffmpeg
def test_broken_upload_fails_transcode(media_root, client, make, session):
    headers = _login(client, make.admin(role="ops").username)
    episode = make.episode(make.drama(), 1)
    body = client.post(
        f"/admin/v1/episodes/{episode.id}/upload-session",
        json={"filename": "bad.mp4", "mime": "video/mp4", "size_bytes": 64, "width": 720, "height": 1280},
        headers=headers,
    ).json()
    _tus_upload(client, "/v1/media/tus", body["headers"], b"not a video" * 6)
    meta = media_worker.process(body["video_id"])
    assert meta["status"] == local_media.STATUS_FAILED and meta["error"]
    session.expire_all()
    assert session.get(Episode, episode.id).status == "transcode_failed"


def test_cover_upload_is_stored_locally(media_root, client, make):
    headers = _login(client, make.admin(role="ops").username)
    drama = make.drama()
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    uploaded = client.post(
        f"/admin/v1/dramas/{drama.id}/cover", files={"file": ("c.png", png, "image/png")}, headers=headers
    )
    assert uploaded.status_code == 200, uploaded.text
    url = uploaded.json()["cover_url"]
    assert url.startswith(f"{MEDIA}/f/")
    stored = media_root / "files" / urlsplit(url).path.removeprefix("/f/")
    assert stored.read_bytes() == png


def test_cleanup_removes_only_old_unreferenced_videos(media_root, make):
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    kept = bunny.create_stream_video("kept")
    orphan = bunny.create_stream_video("orphan")
    fresh = bunny.create_stream_video("fresh")
    for video_id in (kept, orphan):
        local_media.update_meta(video_id, dateUploaded=old)
    episode = make.episode(make.drama(), 1)
    from app.db import get_engine, make_session_factory

    with make_session_factory(get_engine())() as db:
        db.get(Episode, episode.id).bunny_video_id = kept
        db.commit()

    assert media_worker.cleanup_orphans() == [orphan]
    assert set(local_media.list_videos()) == {kept, fresh}


def test_recover_requeues_interrupted_transcode(media_root):
    video_id = bunny.create_stream_video("ep")
    local_media.update_meta(video_id, status=local_media.STATUS_TRANSCODING)
    (media_root / "hls" / f"{video_id}.tmp").mkdir(parents=True)
    media_worker.recover()
    assert bunny.stream_video(video_id)["status"] == local_media.STATUS_UPLOADED
    assert not (media_root / "hls" / f"{video_id}.tmp").exists()
