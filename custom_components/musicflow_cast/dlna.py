"""DLNA / UPnP 控制(MediaRenderer 直接控制)。

不走 MusicFlow 服务端,HA 直接给发现的本地 DLNA 设备发 SOAP:
  SetAVTransportURI / Play / Pause / Stop / Seek / Next / Previous
  SetVolume / SetMute / GetVolume / GetMute
  GetTransportInfo / GetPositionInfo

顺序对齐 MusicFlow 后端 services/dlna/control.ts 的 castToDevice:先 Stop,再
SetAVTransportURI,等 CanPlay,再 Play。所有请求都是对设备控制 URL 的标准 UPnP SOAP。
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .const import REQUEST_TIMEOUT
from .discovery import DlnaDeviceInfo

_LOGGER = logging.getLogger(__name__)

AV_TRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
RENDERING_CONTROL = "urn:schemas-upnp-org:service:RenderingControl:1"

SOAP_ENV = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    "<s:Body>{body}</s:Body></s:Envelope>"
)


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _build_soap(service_type: str, action: str, args: dict[str, str]) -> str:
    inner = "".join(
        f"<{k}>{_xml_escape(v)}</{k}>" for k, v in args.items()
    )
    body = f'<u:{action} xmlns:u="{service_type}">{inner}</u:{action}>'
    return SOAP_ENV.format(body=body)


def _didl_lite(
    title: str,
    artist: str,
    album: str,
    stream_url: str,
    cover_url: str | None,
    mime: str,
    duration: str,
) -> str:
    """构造 DLNA 播放所需的 DIDL-Lite 元数据。"""
    art = ""
    if cover_url:
        art = f'<upnp:albumArtURI>{_xml_escape(cover_url)}</upnp:albumArtURI>'
    res = (
        f'<res protocolInfo="http-get:*:{_xml_escape(mime)}:*" '
        f'duration="{_xml_escape(duration)}">{_xml_escape(stream_url)}</res>'
    )
    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        '<item id="0" parentID="0" restricted="1">'
        f"<dc:title>{_xml_escape(title)}</dc:title>"
        f"<dc:creator>{_xml_escape(artist)}</dc:creator>"
        f"<upnp:artist>{_xml_escape(artist)}</upnp:artist>"
        f"<upnp:album>{_xml_escape(album)}</upnp:album>"
        '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
        f"{res}{art}"
        "</item></DIDL-Lite>"
    )


def _parse_action_response(xml_text: str, action: str) -> dict[str, str]:
    """从 SOAP 响应里抽取 <actionResponse> 下的参数名→值。"""
    out: dict[str, str] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    target = f"{action}Response"
    for el in root.iter():
        if _qname_local(el.tag) == target:
            for child in el:
                out[_qname_local(child.tag)] = (child.text or "").strip()
            return out
    # 有些设备把返回值放在 Body 直接子节点,再兜底扫一遍
    for el in root.iter():
        if _qname_local(el.tag) not in ("Envelope", "Body", target):
            out[_qname_local(el.tag)] = (el.text or "").strip()
    return out


def _qname_local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _sec_to_hms(total: float) -> str:
    total = int(total)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}:{m:02d}:{s:02d}"


@dataclass
class DlnaDeviceState:
    """设备实时状态(由 GetTransportInfo / GetPositionInfo / GetVolume 聚合)。"""

    available: bool = True
    transport_state: str = "STOPPED"   # PLAYING / PAUSED_PLAYBACK / STOPPED / ...
    current_uri: str | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    cover_url: str | None = None
    mime: str = "audio/mpeg"
    duration: float = 0.0       # 秒
    position: float = 0.0       # 秒
    volume: int = 50            # 0-100
    muted: bool = False


@dataclass
class DlnaDevice:
    """一个 DLNA MediaRenderer 设备 + 其控制端点。"""

    info: DlnaDeviceInfo
    state: DlnaDeviceState = field(default_factory=DlnaDeviceState)
    missing_count: int = 0

    @property
    def udn(self) -> str:
        return self.info.udn

    @property
    def name(self) -> str:
        return self.info.name

    async def _soap(
        self, session: aiohttp.ClientSession, url: str | None, service: str, action: str, args: dict[str, str]
    ) -> dict[str, str] | None:
        if not url:
            return None
        envelope = _build_soap(service, action, args)
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{service}#{action}"',
        }
        try:
            async with session.post(
                url, headers=headers, data=envelope,
                ssl=None, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                text = await resp.text()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("DLNA %s %s 失败: %s", action, self.udn, err)
            return None
        if resp.status >= 400:
            return None
        return _parse_action_response(text, action)

    # ---- 播放控制 ----
    async def async_set_uri(
        self, session: aiohttp.ClientSession, stream_url: str, meta: dict[str, Any]
    ) -> bool:
        """SetAVTransportURI(带 DIDL-Lite 元数据)。"""
        didl = _didl_lite(
            title=meta.get("title", "Unknown"),
            artist=meta.get("artist", "Unknown"),
            album=meta.get("album", ""),
            stream_url=stream_url,
            cover_url=meta.get("cover_url"),
            mime=meta.get("mime", "audio/mpeg"),
            duration=_sec_to_hms(meta.get("duration", 0) or 0),
        )
        res = await self._soap(
            session, self.info.av_transport_url, AV_TRANSPORT, "SetAVTransportURI",
            {"InstanceID": "0", "CurrentURI": stream_url, "CurrentURIMetaData": didl},
        )
        return res is not None

    async def async_play(self, session: aiohttp.ClientSession) -> bool:
        res = await self._soap(
            session, self.info.av_transport_url, AV_TRANSPORT, "Play",
            {"InstanceID": "0", "Speed": "1"},
        )
        return res is not None

    async def async_pause(self, session: aiohttp.ClientSession) -> bool:
        res = await self._soap(
            session, self.info.av_transport_url, AV_TRANSPORT, "Pause",
            {"InstanceID": "0"},
        )
        return res is not None

    async def async_stop(self, session: aiohttp.ClientSession) -> bool:
        res = await self._soap(
            session, self.info.av_transport_url, AV_TRANSPORT, "Stop",
            {"InstanceID": "0"},
        )
        return res is not None

    async def async_next(self, session: aiohttp.ClientSession) -> bool:
        res = await self._soap(
            session, self.info.av_transport_url, AV_TRANSPORT, "Next",
            {"InstanceID": "0"},
        )
        return res is not None

    async def async_previous(self, session: aiohttp.ClientSession) -> bool:
        res = await self._soap(
            session, self.info.av_transport_url, AV_TRANSPORT, "Previous",
            {"InstanceID": "0"},
        )
        return res is not None

    async def async_seek(self, session: aiohttp.ClientSession, seconds: float) -> bool:
        res = await self._soap(
            session, self.info.av_transport_url, AV_TRANSPORT, "Seek",
            {"InstanceID": "0", "Unit": "REL_TIME", "Target": _sec_to_hms(seconds)},
        )
        return res is not None

    # ---- 音量 ----
    async def async_set_volume(self, session: aiohttp.ClientSession, volume: int) -> bool:
        res = await self._soap(
            session, self.info.rendering_control_url, RENDERING_CONTROL, "SetVolume",
            {"InstanceID": "0", "Channel": "Master", "DesiredVolume": str(max(0, min(100, int(volume))))},
        )
        return res is not None

    async def async_set_mute(self, session: aiohttp.ClientSession, muted: bool) -> bool:
        res = await self._soap(
            session, self.info.rendering_control_url, RENDERING_CONTROL, "SetMute",
            {"InstanceID": "0", "Channel": "Master", "DesiredMute": "1" if muted else "0"},
        )
        return res is not None

    # ---- 状态轮询 ----
    async def async_update(self, session: aiohttp.ClientSession) -> None:
        """拉取最新状态并写回 self.state。"""
        transport = await self._soap(
            session, self.info.av_transport_url, AV_TRANSPORT, "GetTransportInfo",
            {"InstanceID": "0"},
        )
        if transport is None:
            self.state.available = False
            return
        self.state.available = True
        self.state.transport_state = transport.get("CurrentTransportState", "STOPPED").upper()

        pos = await self._soap(
            session, self.info.av_transport_url, AV_TRANSPORT, "GetPositionInfo",
            {"InstanceID": "0"},
        )
        if pos:
            self.state.current_uri = pos.get("TrackURI")
            self.state.duration = _hms_to_sec(pos.get("TrackDuration", "0:00:00"))
            self.state.position = _hms_to_sec(pos.get("RelTime", "0:00:00"))
            meta = pos.get("TrackMetaData", "")
            if meta:
                _apply_didl(self.state, meta)

        vol = await self._soap(
            session, self.info.rendering_control_url, RENDERING_CONTROL, "GetVolume",
            {"InstanceID": "0", "Channel": "Master"},
        )
        if vol:
            try:
                self.state.volume = int(float(vol.get("CurrentVolume", 50)))
            except (ValueError, TypeError):
                pass

        mute = await self._soap(
            session, self.info.rendering_control_url, RENDERING_CONTROL, "GetMute",
            {"InstanceID": "0", "Channel": "Master"},
        )
        if mute:
            self.state.muted = mute.get("CurrentMute", "0") in ("1", "true", "True")


def _hms_to_sec(hms: str) -> float:
    if not hms:
        return 0.0
    parts = hms.split(":")
    try:
        if len(parts) == 3:
            h, m, s = (float(p) for p in parts)
            return h * 3600 + m * 60 + s
        if len(parts) == 2:
            m, s = (float(p) for p in parts)
            return m * 60 + s
    except ValueError:
        return 0.0
    return 0.0


def _apply_didl(state: DlnaDeviceState, didl: str) -> None:
    """从 TrackMetaData(DIDL-Lite)里抽取标题/艺术家/专辑/封面。"""
    try:
        root = ET.fromstring(didl)
    except ET.ParseError:
        return
    for el in root.iter():
        local = _qname_local(el.tag)
        text = (el.text or "").strip()
        if local == "title":
            state.title = text
        elif local == "artist":
            state.artist = text
        elif local == "album":
            state.album = text
        elif local == "albumArtURI":
            state.cover_url = text
        elif local == "res" and state.mime == "audio/mpeg":
            # protocolInfo 里可能带 mime
            pi = el.get("protocolInfo", "")
            if ":" in pi:
                mime = pi.split(":")[2] or ""
                if mime:
                    state.mime = mime
