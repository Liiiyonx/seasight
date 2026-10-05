"""Video stream URL contract.

The browser must receive a same-origin path by default. Returning the Docker
service name (``go2rtc:1984``) cannot work in a browser and also leaks the
internal topology.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.api.v1.devices import _stream_key, _stream_url
from app.core.config import settings


def test_stream_key_uses_meta_verbatim() -> None:
    device = SimpleNamespace(
        device_id="CAM-MABI-01",
        meta={"stream_key": "CAM-MABI-01"},
    )
    assert _stream_key(device) == "CAM-MABI-01"


def test_stream_url_is_same_origin_by_default(monkeypatch) -> None:
    monkeypatch.setattr(settings, "stream_public_base_url", "")
    device = SimpleNamespace(
        device_id="CAM-MABI-01",
        meta={"stream_key": "CAM-MABI-01"},
    )
    assert _stream_url(device, "stream.flv") == (
        "/stream/api/stream.flv?src=CAM-MABI-01"
    )


def test_stream_url_honours_explicit_public_base(monkeypatch) -> None:
    monkeypatch.setattr(settings, "stream_public_base_url", "https://media.example.cn/")
    device = SimpleNamespace(device_id="CAM-MABI-01", meta={})
    assert _stream_url(device, "webrtc") == (
        "https://media.example.cn/stream/api/webrtc?src=CAM-MABI-01"
    )
