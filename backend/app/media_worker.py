"""本机视频转码 worker（``BUNNY_MODE=local``）。

运行：``cd backend && ../.venv/bin/python -m app.media_worker``（线上由 systemd 服务
``minitv-media`` 托管，低优先级运行）。

循环做三件事：

1. 取最早进入队列（status=1）的视频，用 ffmpeg 转成多清晰度 HLS。一次只转一个：
   服务器只有 2 核，并行只会让每一集都更慢。
2. 转完写回 ``meta.json``（status=4 成功 / 5 失败），然后立即对账数据库，让分集
   从「转码中」进入「审核中」，不必等 API 每两分钟一次的轮询。
3. 每小时清理一次没有被任何分集引用、且创建已超过一天的视频（被替换或删除的旧视频），
   释放磁盘。

输出目录结构和 Bunny 一致（``{h}p/video.m3u8``）：App 按目录名识别清晰度。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.modules.common import bunny, local_media

log = logging.getLogger("minitv.media")

POLL_SECONDS = 3
CLEANUP_INTERVAL_SECONDS = 3600
ORPHAN_GRACE = timedelta(days=1)
SEGMENT_SECONDS = 6
AUDIO_KBPS = 96
# 各档码率上限（kbps）。crf 23 下短剧的实际码率通常远低于上限。
MAXRATE_KBPS = {240: 500, 360: 900, 480: 1300, 540: 1600, 720: 2600, 1080: 5000, 1440: 9000, 2160: 16000}


class TranscodeError(Exception):
    pass


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------


def _rotation(stream: dict) -> int:
    tags = stream.get("tags") or {}
    if tags.get("rotate"):
        return abs(int(float(tags["rotate"])))
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            return abs(int(float(side["rotation"])))
    return 0


def probe(path: Path) -> dict:
    """原片信息：显示方向上的宽高（已考虑旋转）、时长、是否有音轨。"""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise TranscodeError(f"ffprobe failed: {result.stderr.strip()[-300:]}")
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise TranscodeError("no video stream in upload")
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if _rotation(video) % 180 == 90:
        width, height = height, width
    if width <= 0 or height <= 0:
        raise TranscodeError("cannot read video size")
    duration = float((data.get("format") or {}).get("duration") or video.get("duration") or 0)
    return {
        "width": width,
        "height": height,
        "duration": duration,
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def ladder(width: int, height: int, targets: list[int]) -> list[int]:
    """不放大：只保留不超过原片短边的档位；原片比最低档还小时按原尺寸转一档。"""
    short = min(width, height)
    chosen = [target for target in targets if target <= short]
    return chosen or [max(2, short - short % 2)]


def encode(src: Path, out_dir: Path, rung: int) -> None:
    """转一档 HLS。竖屏把宽缩到 rung，横屏把高缩到 rung（即短边 = rung）。"""
    out_dir.mkdir(parents=True)
    rate = MAXRATE_KBPS.get(rung) or max(400, int(rung * 3.6))
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", f"scale=w='if(lt(iw,ih),{rung},-2)':h='if(lt(iw,ih),-2,{rung})'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-profile:v", "main", "-pix_fmt", "yuv420p",
        "-maxrate", f"{rate}k", "-bufsize", f"{rate * 2}k",
        # 每 2 秒一个关键帧，分片才能在 6 秒边界切开。
        "-force_key_frames", "expr:gte(t,n_forced*2)",
        "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-ac", "2", "-ar", "44100",
        "-f", "hls", "-hls_time", str(SEGMENT_SECONDS),
        "-hls_playlist_type", "vod", "-hls_list_size", "0",
        "-hls_segment_filename", str(out_dir / "seg_%03d.ts"),
        str(out_dir / "video.m3u8"),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise TranscodeError(f"ffmpeg {rung}p failed: {result.stderr.strip()[-500:]}")


def variant_stats(out_dir: Path) -> dict:
    """主清单需要的峰值 / 平均码率和实际分辨率。"""
    lines = (out_dir / "video.m3u8").read_text().splitlines()
    peak = 0.0
    total_bits = 0
    total_seconds = 0.0
    first_segment = None
    for index, line in enumerate(lines):
        if not line.startswith("#EXTINF:"):
            continue
        seconds = float(line[len("#EXTINF:"):].split(",")[0])
        segment = out_dir / lines[index + 1].strip()
        first_segment = first_segment or segment
        bits = segment.stat().st_size * 8
        peak = max(peak, bits / max(seconds, 0.5))
        total_bits += bits
        total_seconds += seconds
    if first_segment is None:
        raise TranscodeError(f"empty playlist in {out_dir.name}")
    size = probe(first_segment)
    return {
        "peak": int(peak * 1.1),
        "average": int(total_bits / max(total_seconds, 0.5)),
        "width": size["width"],
        "height": size["height"],
    }


def write_master(hls_root: Path, variants: list[tuple[int, dict]]) -> None:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for rung, stats in variants:
        lines.append(
            f"#EXT-X-STREAM-INF:BANDWIDTH={stats['peak']},AVERAGE-BANDWIDTH={stats['average']},"
            f"RESOLUTION={stats['width']}x{stats['height']}"
        )
        lines.append(f"{rung}p/video.m3u8")
    (hls_root / "playlist.m3u8").write_text("\n".join(lines) + "\n")


def transcode(video_id: str) -> dict:
    """原片 → hls/{id}/。先写到 .tmp 目录，全部成功后再整体换上，播放端不会看到半成品。"""
    src = local_media.source_path(video_id)
    info = probe(src)
    rungs = ladder(info["width"], info["height"], local_media.renditions())
    final = local_media.hls_dir(video_id)
    tmp = final.with_name(final.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        variants = []
        for done, rung in enumerate(rungs, start=1):
            encode(src, tmp / f"{rung}p", rung)
            variants.append((rung, variant_stats(tmp / f"{rung}p")))
            local_media.update_meta(video_id, encodeProgress=int(done * 99 / len(rungs)))
        write_master(tmp, variants)
        shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    top = variants[-1][1]
    return {
        "length": max(1, round(info["duration"])),
        "width": top["width"],
        "height": top["height"],
        "availableResolutions": ",".join(f"{rung}p" for rung in rungs),
    }


# ---------------------------------------------------------------------------
# 队列
# ---------------------------------------------------------------------------


def next_job() -> str | None:
    queued = [
        (meta.get("dateQueued") or meta.get("dateUploaded") or "", video_id)
        for video_id, meta in local_media.list_videos().items()
        if int(meta.get("status") or 0) == local_media.STATUS_UPLOADED
    ]
    return min(queued)[1] if queued else None


def process(video_id: str) -> dict:
    local_media.update_meta(
        video_id, status=local_media.STATUS_TRANSCODING, encodeProgress=0, transcodeStartedAt=local_media.now_iso()
    )
    started = time.monotonic()
    try:
        result = transcode(video_id)
    except (TranscodeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        log.warning("transcode %s failed: %s", video_id, exc)
        meta = local_media.update_meta(
            video_id, status=local_media.STATUS_FAILED, encodeProgress=0, error=str(exc)[-500:]
        )
    else:
        meta = local_media.update_meta(
            video_id,
            status=local_media.STATUS_FINISHED,
            encodeProgress=100,
            error="",
            transcodeSeconds=round(time.monotonic() - started),
            **result,
        )
        local_media.source_path(video_id).unlink(missing_ok=True)
        log.info("transcoded %s %s in %ss", video_id, result["availableResolutions"], meta["transcodeSeconds"])
    reconcile()
    return meta


def recover() -> None:
    """上次进程中断（重启、部署）时正在转的视频重新排队，清掉没写完的临时目录。"""
    for video_id, meta in local_media.list_videos().items():
        if int(meta.get("status") or 0) == local_media.STATUS_TRANSCODING:
            local_media.update_meta(video_id, status=local_media.STATUS_UPLOADED, encodeProgress=0)
            log.info("requeued interrupted transcode %s", video_id)
    for leftover in (local_media.media_root() / "hls").glob("*.tmp"):
        shutil.rmtree(leftover, ignore_errors=True)


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------


def _session():
    from app.db import get_engine, make_session_factory

    return make_session_factory(get_engine())()


def reconcile() -> None:
    """把转码结果同步到分集状态（转码中 → 审核中 / 转码失败）。"""
    from app.modules.ops.flow_router import reconcile_transcoding

    session = _session()
    try:
        result = reconcile_transcoding(session, local_media.list_videos())
        if result["advanced"] or result["failed"]:
            log.info("reconciled episodes %s", result)
    except Exception:
        session.rollback()
        log.exception("reconcile failed")
    finally:
        session.close()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def cleanup_orphans(now: datetime | None = None) -> list[str]:
    """删除没有被任何分集引用、创建超过一天的视频。查库失败时什么都不删。"""
    from sqlalchemy import select

    from app.modules.content.models import Episode

    session = _session()
    try:
        referenced = set(session.scalars(select(Episode.bunny_video_id).where(Episode.bunny_video_id != "")).all())
    finally:
        session.close()
    cutoff = (now or datetime.now(timezone.utc)) - ORPHAN_GRACE
    removed = []
    for video_id, meta in local_media.list_videos().items():
        if video_id in referenced or int(meta.get("status") or 0) == local_media.STATUS_TRANSCODING:
            continue
        created = _parse_time(meta.get("dateUploaded"))
        if created is None or created > cutoff:
            continue
        local_media.delete_video(video_id)
        removed.append(video_id)
    if removed:
        log.info("removed %d orphan video(s): %s", len(removed), ", ".join(removed))
    return removed


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from app.db import load_env

    load_env()
    if bunny.mode() != "local":
        log.info("BUNNY_MODE=%s, media worker idle (switch to local and restart to enable)", bunny.mode())
        while True:
            time.sleep(3600)
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} not found on PATH")
    for sub in ("videos", "hls", "files"):
        (local_media.media_root() / sub).mkdir(parents=True, exist_ok=True)
    recover()
    log.info("media worker started, root=%s renditions=%s", local_media.media_root(), local_media.renditions())
    last_cleanup = 0.0
    while True:
        try:
            video_id = next_job()
            if video_id:
                process(video_id)
                continue
            if time.monotonic() - last_cleanup > CLEANUP_INTERVAL_SECONDS:
                last_cleanup = time.monotonic()
                cleanup_orphans()
        except Exception:
            log.exception("media worker loop error")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
