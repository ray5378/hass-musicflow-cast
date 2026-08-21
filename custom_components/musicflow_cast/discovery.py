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
import socket
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
SSDP_ST_ALL = "ssdp:all"


def _m_search(st: str) -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_GROUP}:{SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 3\r\n"
        f"ST: {st}\r\n"
        "\r\n"
    ).encode("utf-8")


# 两条 M-SEARCH:精确 ST + 通配 ssdp:all。部分消费级设备(如惠威 H5 MKII 这类
# 音箱)只对 ssdp:all 响应,或响应 ST 与自己声明的不完全一致 —— 宽匹配兜底。
M_SEARCH = _m_search(SSDP_ST)
M_SEARCH_ALL = _m_search(SSDP_ST_ALL)

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


def parse_ssdp_notify(text: str) -> dict[str, str] | None:
    """解析一条 SSDP NOTIFY(ssdp:alive / ssdp:byebye)。

    返回 {nts, udn, location};非 NOTIFY 或无关 NTS 返回 None。
    USN 形如 'uuid:<udn>::urn:...' → udn 取 '::' 之前部分。
    """
    lines = text.splitlines()
    if not lines or not lines[0].startswith("NOTIFY"):
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
    nts = headers.get("nts", "")
    if nts not in ("ssdp:alive", "ssdp:byebye"):
        return None
    usn = headers.get("usn", "")
    udn = usn.split("::", 1)[0] if "::" in usn else usn
    if not udn:
        return None
    return {"nts": nts, "udn": udn, "location": headers.get("location", "")}


class _SsdpProbeProtocol(asyncio.DatagramProtocol):
    """收集 M-SEARCH 响应中的 LOCATION。

    用 DatagramProtocol 回调接收 UDP,而不是 `transport.get_extra_info('socket')`
    配合 `loop.sock_recv()` —— 后者在 Python 3.12+/HA 2026.2 里拿到的是
    `TransportSocket`(无 recv),导致 AttributeError 被容错吞掉、永远发现不到设备。
    """

    def __init__(self) -> None:
        self.locations: list[str] = []
        self.signal = asyncio.Event()

    def datagram_received(self, data: bytes, addr: Any) -> None:
        text = data.decode("utf-8", "ignore")
        if "location:" not in text.lower():
            return
        for line in text.splitlines():
            if line.lower().startswith("location:"):
                loc = line.split(":", 1)[1].strip()
                if loc:
                    self.locations.append(loc)
                break
        self.signal.set()

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("SSDP 接收错误: %s", exc)


async def discover_renderers(session: aiohttp.ClientSession, timeout: float = 6.0) -> list[DlnaDeviceInfo]:
    """主动发一轮 M-SEARCH,收集局域网内 MediaRenderer。

    返回发现的设备列表(按 UDN 去重)。同一台设备可能回多个响应,调用方需去重。
    失败(网络受限 / 无广播权限 / 容器隔离)时返回空列表并记 warning —— 发现失败
    只是"暂时没设备",绝不能冒泡让集成 setup 失败。
    """
    loop = asyncio.get_running_loop()
    devices: dict[str, DlnaDeviceInfo] = {}
    locations_seen: set[str] = set()

    transport = None
    try:
        # 单个 UDP socket 既发 M-SEARCH 又收响应;多播/广播在容器、隔离网络下可能
        # 抛 OSError/PermissionError,必须容错。
        protocol = _SsdpProbeProtocol()
        sock = await loop.create_datagram_endpoint(
            lambda: protocol,
            family=socket.AF_INET,
            allow_broadcast=True,
        )
        transport = sock[0]

        raw = transport.get_extra_info("socket")
        if raw is not None:
            try:
                # 多播 TTL:默认 TTL 在部分系统为 0,设备收不到 M-SEARCH
                raw.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            except OSError:
                pass
            try:
                # 加入 SSDP 多播组:才能收到设备主动广播的 NOTIFY(ssdp:alive)。
                # 很多消费级 DLNA 设备(音箱/电视)只在联网时广播 NOTIFY、对
                # M-SEARCH 响应不可靠 —— 不加组就永远看不到它们。接口用 INADDR_ANY,
                # 让系统选默认网卡;失败只降级(仍能收单播响应),不致命。
                mreq = socket.inet_aton(SSDP_GROUP) + socket.inet_aton("0.0.0.0")
                raw.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError as err:
                _LOGGER.debug("加入 SSDP 多播组失败(仅影响 NOTIFY 监听): %s", err)

        try:
            transport.sendto(M_SEARCH, SSDP_TARGET)
            # ssdp:all 兜底:部分设备只对通配 ST 响应
            transport.sendto(M_SEARCH_ALL, SSDP_TARGET)
        except OSError as err:
            _LOGGER.warning("SSDP M-SEARCH 发送失败(网络受限?): %s", err)
            return []

        # 等待响应直到超时(同一设备可能回多个响应,location 去重放在后面)
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                await asyncio.wait_for(
                    protocol.signal.wait(), timeout=max(0.05, deadline - loop.time())
                )
                protocol.signal.clear()
            except asyncio.TimeoutError:
                break
            except Exception:  # noqa: BLE001
                break
        locations_seen.update(protocol.locations)
    except Exception as err:  # noqa: BLE001
        # create_datagram_endpoint 等失败:无 SO_BROADCAST 权限 / 容器网络限制
        _LOGGER.warning("SSDP 发现 socket 创建失败(网络/容器限制?): %s", err)
        return []
    finally:
        if transport is not None:
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
