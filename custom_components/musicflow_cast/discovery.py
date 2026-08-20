"""本机 LAN 的 SSDP 发现(MediaRenderer)。

纯 asyncio + 标准库 socket,零第三方依赖,与 MusicFlow「零依赖」风格一致。
向 239.255.255.250:1900 发 M-SEARCH,收集局域网内 urn:schemas-upnp-org:device:
MediaRenderer:1 的响应,解析每个设备的 location(description.xml),取出
friendlyName / UDN / AVTransport / RenderingControl 控制 URL。

注意:这是在 **HA 所在局域网** 发现 DLNA,与 MusicFlow 服务器无关 —— 这正是新建
此集成的根本原因(MusicFlow 服务端永远发现不了 HA 这边的设备)。
"""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import aiohttp

from .const import REQUEST_TIMEOUT

_LOGGER = logging.getLogger(__name__)

SSDP_GROUP = "239.255.255.250"
SSDP_PORT = 1900
SSDP_TARGET = (SSDP_GROUP, SSDP_PORT)
SSDP_ST = "urn:schemas-upnp-org:device:MediaRenderer:1"
M_SEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    f"HOST: {SSDP_GROUP}:{SSDP_PORT}\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 3\r\n"
    f"ST: {SSDP_ST}\r\n"
    "\r\n"
).encode("utf-8")

# UPnP 命名空间
_NS = {
    "upnp": "urn:schemas-upnp-org:device-1-0",
    "service": "urn:schemas-upnp-org:service-1-0",
}


@dataclass(slots=True)
class DlnaDeviceInfo:
    """SSDP 发现到的 DLNA MediaRenderer 描述。"""

    udn: str
    name: str
    location: str
    av_transport_url: str | None = None
    rendering_control_url: str | None = None


def _qname(tag: str) -> str:
    """把 {ns}local 形式的 tag 转成 local(ElementTree 默认带命名空间前缀)。"""
    return tag.split("}")[-1] if "}" in tag else tag


async def _fetch_description(location: str, session: aiohttp.ClientSession) -> dict[str, Any] | None:
    """拉取设备的 description.xml 并解析关键字段。"""
    try:
        async with session.get(
            location, ssl=None, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        ) as resp:
            if resp.status != 200:
                return None
            text = await resp.text()
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("拉取设备描述 %s 失败: %s", location, err)
        return None

    try:
        root = ET.fromstring(text)
    except ET.ParseError as err:
        _LOGGER.debug("解析设备描述 %s 失败: %s", location, err)
        return None

    udn = ""
    name = location
    av_transport = None
    rendering_control = None

    # device 节点
    device = None
    for el in root.iter():
        if _qname(el.tag) == "device":
            device = el
            break
    if device is not None:
        for child in device:
            tag = _qname(child.tag)
            if tag == "UDN":
                udn = (child.text or "").strip()
            elif tag == "friendlyName":
                name = (child.text or "").strip() or name

    # serviceList:找 AVTransport / RenderingControl 的 controlURL
    for el in root.iter():
        if _qname(el.tag) == "service":
            svc_type = ""
            ctrl = ""
            for child in el:
                tag = _qname(child.tag)
                if tag == "serviceType":
                    svc_type = (child.text or "").strip()
                elif tag == "controlURL":
                    ctrl = (child.text or "").strip()
            if not ctrl:
                continue
            if "AVTransport" in svc_type and av_transport is None:
                av_transport = ctrl
            elif "RenderingControl" in svc_type and rendering_control is None:
                rendering_control = ctrl

    if not udn or not av_transport:
        # 不是可用的 MediaRenderer(缺 AVTransport)
        return None

    # controlURL 可能是相对路径,需要相对 location 补全
    base = location
    if av_transport and not av_transport.startswith("http"):
        av_transport = _join_url(base, av_transport)
    if rendering_control and not rendering_control.startswith("http"):
        rendering_control = _join_url(base, rendering_control)

    return {
        "udn": udn,
        "name": name,
        "location": location,
        "av_transport_url": av_transport,
        "rendering_control_url": rendering_control,
    }


def _join_url(base: str, relative: str) -> str:
    from urllib.parse import urljoin

    return urljoin(base, relative)


async def discover_renderers(session: aiohttp.ClientSession, timeout: float = 4.0) -> list[DlnaDeviceInfo]:
    """主动发一轮 M-SEARCH,收集局域网内 MediaRenderer。

    返回发现的设备列表(按 UDN 去重)。同一台设备可能回多个响应,调用方需去重。
    """
    loop = asyncio.get_running_loop()
    devices: dict[str, DlnaDeviceInfo] = {}
    locations_seen: set[str] = set()

    # 用单个 UDP socket 既发 M-SEARCH 又收响应(绑定到通配地址 + 随机端口)
    sock = await loop.create_datagram_endpoint(
        asyncio.DatagramProtocol,
        family=__import__("socket").AF_INET,
        allow_broadcast=True,
    )
    transport = sock[0]
    protocol = sock[1]

    try:
        transport.sendto(M_SEARCH, SSDP_TARGET)
        sock = transport.get_extra_info("socket")
        if sock is None:
            return list(devices.values())

        # 接收响应:轮询 socket 直到超时(同一设备可能回多个响应,去重在后面做)
        end = loop.time() + timeout
        while loop.time() < end:
            try:
                data, _addr = await asyncio.wait_for(
                    loop.sock_recv(sock, 4096),
                    timeout=max(0.1, end - loop.time()),
                )
            except asyncio.TimeoutError:
                break
            except OSError:
                break
            text = data.decode("utf-8", "ignore")
            if "LOCATION:" not in text and "location:" not in text:
                continue
            for line in text.splitlines():
                low = line.lower()
                if low.startswith("location:"):
                    location = line.split(":", 1)[1].strip()
                    if location and location not in locations_seen:
                        locations_seen.add(location)
                    break
    finally:
        try:
            transport.close()
        except Exception:  # noqa: BLE001
            pass

    # 并发拉取每个 location 的 description.xml
    results = await asyncio.gather(
        *(_fetch_description(loc, session) for loc in locations_seen),
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, dict) and r.get("udn"):
            info = DlnaDeviceInfo(
                udn=r["udn"],
                name=r["name"],
                location=r["location"],
                av_transport_url=r.get("av_transport_url"),
                rendering_control_url=r.get("rendering_control_url"),
            )
            devices[info.udn] = info

    return list(devices.values())
