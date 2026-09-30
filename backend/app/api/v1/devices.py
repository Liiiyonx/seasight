"""设备与机器人相关接口。"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_session
from app.core.exceptions import ApiResponse
from app.repositories import DeviceRepository, TaskRepository
from app.schemas import DeviceOut, RobotOut

router = APIRouter()


def _stream_key(device) -> str:
    """返回 go2rtc 流 key；必须与 deploy/go2rtc/go2rtc.yaml 完全一致。"""
    if device.meta and device.meta.get("stream_key"):
        return str(device.meta["stream_key"]).strip()
    return str(device.device_id).strip()


def _stream_url(device, endpoint: str) -> str:
    """构造浏览器可达的视频地址。

    默认返回同源相对路径，由前端 Nginx 的 /stream/ 代理到 go2rtc。只有
    显式配置 STREAM_PUBLIC_BASE_URL 时才返回该基地址，避免把容器内的
    ``go2rtc:1984`` 暴露给浏览器。
    """
    key = quote(_stream_key(device), safe="")
    path = f"/stream/api/{endpoint}?src={key}"
    base = settings.stream_public_base_url.rstrip("/")
    return f"{base}{path}" if base else path


async def _to_device_out(session: AsyncSession, device) -> DeviceOut:
    row = (
        await session.execute(
            select(func.ST_X(device.location), func.ST_Y(device.location))
        )
    ).first()
    lng = float(row[0]) if row and row[0] is not None else 0.0
    lat = float(row[1]) if row and row[1] is not None else 0.0
    return DeviceOut(
        device_id=device.device_id,
        device_type=device.device_type,
        name=device.name,
        lng=lng,
        lat=lat,
        status=device.status,
        last_heartbeat=device.last_heartbeat,
        stream_url=(
            _stream_url(device, "stream.flv")
            if device.device_type == "shore_camera"
            else device.stream_url
        ),
        meta=device.meta or {},
    )


@router.get("", response_model=ApiResponse[list], summary="设备列表")
async def list_devices(
    device_type: str | None = Query(None, description="shore_camera / drone / robot"),
    status: str | None = Query(None, description="online / offline / fault"),
    session: AsyncSession = Depends(get_session),
):
    """查询设备列表（设备管理页数据源）。"""
    devices = await DeviceRepository(session).list_devices(
        device_type=device_type, status=status
    )
    items = [await _to_device_out(session, d) for d in devices]
    return ApiResponse.ok([i.model_dump() for i in items])


@router.get("/{device_id}", response_model=ApiResponse[DeviceOut], summary="设备详情")
async def get_device(device_id: str, session: AsyncSession = Depends(get_session)):
    from app.core.exceptions import NotFoundError

    device = await DeviceRepository(session).get_by_device_id(device_id)
    if device is None:
        raise NotFoundError(f"设备 {device_id} 不存在", code=2001)
    return ApiResponse.ok(await _to_device_out(session, device))


@router.get("/{device_id}/stream", response_model=ApiResponse[dict], summary="获取视频流地址")
async def get_stream_url(device_id: str, session: AsyncSession = Depends(get_session)):
    """获取设备视频流播放地址。

    返回 go2rtc 转发地址；前端用 mpegts.js 播放 HTTP-FLV，
    或直接用 WebRTC（延迟更低）。
    """
    from app.core.exceptions import NotFoundError

    device = await DeviceRepository(session).get_by_device_id(device_id)
    if device is None:
        raise NotFoundError(f"设备 {device_id} 不存在", code=2001)

    return ApiResponse.ok(
        {
            "device_id": device_id,
            "flv_url": _stream_url(device, "stream.flv"),
            "webrtc_url": _stream_url(device, "webrtc"),
            "hls_url": _stream_url(device, "stream.m3u8"),
        }
    )
