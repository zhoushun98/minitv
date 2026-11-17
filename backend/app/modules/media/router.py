"""TUS 上传接收端（``BUNNY_MODE=local``），协议行为对齐 Bunny 的 ``/tusupload``。

客户端（后台、合作方后台、CLI）拿到的上传会话是 ``upload_url`` 加四个授权头
（见 ``local_media.tus_headers``），它们在 POST / HEAD / PATCH 上原样携带：

* ``POST /v1/media/tus``：带 ``Upload-Length`` 建会话，返回 ``Location: /v1/media/tus/{id}``。
* ``HEAD /v1/media/tus/{id}``：返回 ``Upload-Offset``，用于断点续传。
* ``PATCH /v1/media/tus/{id}``：从 ``Upload-Offset`` 处追加字节；收齐后原片进入转码队列。

后台在浏览器里跨域访问这里。万一读不到 ``Location``，会直接 PATCH 建会话的 URL，
所以 ``PATCH /v1/media/tus`` 也按 ``VideoId`` 头处理。
"""

from __future__ import annotations

import fcntl
import os

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from app.modules.common import bunny, local_media

router = APIRouter(prefix="/v1/media", tags=["media"])

TUS_VERSION = {"Tus-Resumable": "1.0.0"}


def _authorize(request: Request, video_id: str) -> str:
    if not bunny.local():
        raise HTTPException(status_code=404, detail="local media is disabled")
    if not local_media.VIDEO_ID_RE.fullmatch(video_id or ""):
        raise HTTPException(status_code=400, detail="invalid video id")
    if not local_media.verify_tus(
        video_id,
        request.headers.get("AuthorizationSignature", ""),
        request.headers.get("AuthorizationExpire", ""),
    ):
        raise HTTPException(status_code=403, detail="bad or expired upload signature")
    return video_id


def _state(video_id: str) -> tuple[int, int, bool]:
    try:
        return local_media.upload_state(video_id)
    except bunny.BunnyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _int_header(request: Request, name: str) -> int:
    try:
        value = int(request.headers.get(name, ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{name} header required") from exc
    if value < 0:
        raise HTTPException(status_code=400, detail=f"{name} must not be negative")
    return value


@router.post("/tus")
async def tus_create(request: Request) -> Response:
    video_id = _authorize(request, request.headers.get("VideoId", ""))
    length = _int_header(request, "Upload-Length")
    try:
        local_media.begin_upload(video_id, length)
    except bunny.BunnyError as exc:
        message = str(exc)
        status = 413 if "too large" in message else 404 if "404" in message else 400
        raise HTTPException(status_code=status, detail=message) from exc
    return Response(status_code=201, headers={"Location": f"/v1/media/tus/{video_id}", **TUS_VERSION})


@router.head("/tus/{video_id}")
def tus_head(video_id: str, request: Request) -> Response:
    _authorize(request, video_id)
    offset, length, _ = _state(video_id)
    return Response(
        status_code=200,
        headers={
            "Upload-Offset": str(offset),
            "Upload-Length": str(length),
            "Cache-Control": "no-store",
            **TUS_VERSION,
        },
    )


@router.patch("/tus/{video_id}")
async def tus_patch(video_id: str, request: Request) -> Response:
    return await _receive(_authorize(request, video_id), request)


@router.patch("/tus")
async def tus_patch_by_header(request: Request) -> Response:
    return await _receive(_authorize(request, request.headers.get("VideoId", "")), request)


async def _receive(video_id: str, request: Request) -> Response:
    claimed = _int_header(request, "Upload-Offset")
    offset, length, complete = _state(video_id)
    if complete:
        return Response(status_code=204, headers={"Upload-Offset": str(length), **TUS_VERSION})
    if length <= 0:
        raise HTTPException(status_code=409, detail="create the upload with POST first")

    part = local_media.part_path(video_id)
    handle = await run_in_threadpool(open, part, "r+b" if part.exists() else "w+b")
    try:
        try:
            # 同一个视频同时只允许一个 PATCH，避免两路写交错。
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HTTPException(status_code=409, detail="upload already in progress") from exc
        offset = os.fstat(handle.fileno()).st_size
        if claimed == 0 and offset > 0:
            # 后台每次都从 0 开始传：视为重新上传，丢掉上次没传完的部分。
            await run_in_threadpool(handle.truncate, 0)
            offset = 0
        elif claimed != offset:
            raise HTTPException(
                status_code=409,
                detail=f"offset mismatch: server has {offset}",
                headers={"Upload-Offset": str(offset), **TUS_VERSION},
            )
        handle.seek(offset)
        written = offset
        try:
            async for chunk in request.stream():
                if not chunk:
                    continue
                if written + len(chunk) > length:
                    await run_in_threadpool(handle.truncate, offset)
                    raise HTTPException(status_code=413, detail="body exceeds Upload-Length")
                await run_in_threadpool(handle.write, chunk)
                written += len(chunk)
        except ClientDisconnect:
            # 连接断了：已写入的部分保留，客户端可以 HEAD 后续传。
            pass
        await run_in_threadpool(handle.flush)
        await run_in_threadpool(os.fsync, handle.fileno())
    finally:
        handle.close()

    if written == length:
        local_media.finish_upload(video_id)
    return Response(status_code=204, headers={"Upload-Offset": str(written), **TUS_VERSION})
