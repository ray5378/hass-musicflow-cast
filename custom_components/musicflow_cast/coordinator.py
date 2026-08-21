"""协调器:持有 MusicFlow 客户端 + aiohttp 会话,负责 SSDP 发现与设备注册表。

HA 当「客户端节点」:
  - MusicFlow(反代 HTTPS)只在浏览曲库 / 拼流地址时用到 → 经 client 发 OpenSubsonic。
  - DLNA 设备在 **HA 本机 LAN** 发现:协调器周期性发 M-SEARCH,把新设备加进
    self.devices,丢失超过阈值的设备移出并通知实体层。
  - 设备的实时状态轮询在 media_player 实体里各自做(经典 HA 模式),协调器不插手。

设备注册表变化通过 _on_devices_changed 回调广播,实体层据此增删实体。
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any, Callable

import aiohttp

from .const import DEVICE_MISSING_LIMIT, DISCOVERY_INTERVAL
from .discovery import (
    DlnaDeviceInfo,
    SSDP_GROUP,
    SSDP_PORT,
    _fetch_description,
    discover_renderers,
    parse_ssdp_notify,
)
from .dlna import DlnaDevice

_LOGGER = logging.getLogger(__name__)

DeviceChangedCallback = Callable[[], Any]


class _SsdpNotifyProtocol(asyncio.DatagramProtocol):
    """常驻监听 SSDP NOTIFY,把 alive/byebye 交给 coordinator 处理。"""

    def __init__(self, on_notify: Callable[[dict[str, str]], None]) -> None:
        self._on_notify = on_notify

    def datagram_received(self, data: bytes, addr: Any) -> None:
        info = parse_ssdp_notify(data.decode("utf-8", "ignore"))
        if not info:
            return
        try:
            self._on_notify(info)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("处理 SSDP NOTIFY 异常")

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("SSDP 监听错误: %s", exc)


class MusicFlowCastCoordinator:
    """SSDP 发现 + DLNA 设备注册表。"""

    def __init__(
        self,
        hass: Any,  # HomeAssistant
        session: aiohttp.ClientSession,
        client: Any,  # MusicFlowClient
    ) -> None:
        self.hass = hass
        self.session = session
        self.client = client
        self.devices: dict[str, DlnaDevice] = {}
        self._listeners: list[DeviceChangedCallback] = []
        self._discovery_task: asyncio.Task | None = None
        self._notify_task: asyncio.Task | None = None
        self._started = False

    # ==================== 生命周期 ====================
    async def async_start(self) -> None:
        if self._started:
            return
        self._started = True
        self._discovery_task = self.hass.async_create_task(self._discovery_loop())
        # 常驻监听 SSDP NOTIFY:消费级设备(如惠威 H5 MKII)只广播 alive 不响应
        # M-SEARCH,不监听就会被当离线移除。启动失败只降级,不致命。
        self._notify_task = self.hass.async_create_task(self._notify_loop())
        # 首轮发现先跑一次,让实体能尽快出现;失败不致命(如网络/容器限制),
        # 后台发现循环会持续重试,绝不向 setup 冒泡。
        try:
            await self.async_discover()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("首轮 DLNA 发现失败(后台将自动重试)")

    async def async_shutdown(self) -> None:
        for task_name in ("_discovery_task", "_notify_task"):
            task = getattr(self, task_name, None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, task_name, None)
        self._started = False

    def async_on_devices_changed(self, cb: DeviceChangedCallback) -> None:
        """注册设备变化回调。回调必须是同步的(在事件循环内直接调用)。"""
        self._listeners.append(cb)

    def _notify(self) -> None:
        # 注意:listeners 都是同步回调(media_player 的 reconcile 已改同步),
        # 直接调用即可 —— 若注册 async 回调会留下 never-awaited coroutine。
        for cb in self._listeners:
            cb()

    # ==================== 发现循环 ====================
    async def _discovery_loop(self) -> None:
        while True:
            try:
                await self.async_discover()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("DLNA 发现循环异常")
            await asyncio.sleep(DISCOVERY_INTERVAL)

    # ==================== NOTIFY 常驻监听(保活) ====================
    async def _notify_loop(self) -> None:
        """常驻监听 SSDP NOTIFY(ssdp:alive / ssdp:byebye)。

        消费级 DLNA 设备(如惠威 H5 MKII)只在联网时广播一次 alive、不响应
        M-SEARCH —— 纯拉取模式几轮后会把它们当离线移除(实体消失、提示
        "不再提供此实体")。监听 NOTIFY 是保活的关键。启动失败只降级。
        """
        loop = asyncio.get_running_loop()
        transport = None
        try:
            protocol = _SsdpNotifyProtocol(self._on_notify)
            sock = await loop.create_datagram_endpoint(
                lambda: protocol,
                local_addr=("0.0.0.0", SSDP_PORT),
                allow_broadcast=True,
                reuse_address=True,
            )
            transport = sock[0]
            raw = transport.get_extra_info("socket")
            if raw is not None:
                try:
                    mreq = socket.inet_aton(SSDP_GROUP) + socket.inet_aton("0.0.0.0")
                    raw.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                except OSError as err:
                    _LOGGER.warning("加入 SSDP 多播组失败,NOTIFY 保活受限: %s", err)
            _LOGGER.info("SSDP NOTIFY 监听已启动(端口 %d)", SSDP_PORT)
            # 常驻等待回调;任务被 cancel 时抛出 CancelledError 退出
            await asyncio.Future()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("SSDP NOTIFY 监听启动失败(降级为纯 M-SEARCH): %s", err)
        finally:
            if transport is not None:
                try:
                    transport.close()
                except Exception:  # noqa: BLE001
                    pass

    def _on_notify(self, info: dict[str, str]) -> None:
        """同步处理一条 NOTIFY(alive 保活 / byebye 移除)。"""
        udn = info["udn"]
        if info["nts"] == "ssdp:alive":
            existing = self.devices.get(udn)
            if existing is not None:
                # 设备还在 → 保活并恢复可用(状态轮询失败可能已把它标不可用)
                existing.missing_count = 0
                existing.state.available = True
                return
            location = info.get("location")
            if location:
                # 未知设备:异步拉 description 补齐控制端点后新增
                self.hass.async_create_task(self._add_from_notify(location))
        elif info["nts"] == "ssdp:byebye":
            if udn in self.devices:
                _LOGGER.info("DLNA 设备下线(byebye): %s", udn)
                self.devices.pop(udn, None)
                self._notify()

    async def _add_from_notify(self, location: str) -> None:
        """从 NOTIFY 的 LOCATION 拉 description,把未知设备加入注册表。"""
        try:
            parsed = await _fetch_description(location, self.session)
        except Exception:  # noqa: BLE001
            parsed = None
        if not parsed or not parsed.get("udn"):
            return
        udn = parsed["udn"]
        if udn in self.devices:
            self.devices[udn].missing_count = 0
            self.devices[udn].state.available = True
            return
        info = DlnaDeviceInfo(
            udn=udn,
            name=parsed["name"],
            location=parsed["location"],
            av_transport_url=parsed.get("av_transport_url"),
            rendering_control_url=parsed.get("rendering_control_url"),
        )
        self.devices[udn] = DlnaDevice(info=info)
        _LOGGER.info("SSDP NOTIFY 发现 DLNA 设备: %s (%s)", info.name, udn)
        self._notify()

    async def async_discover(self) -> None:
        """跑一轮 M-SEARCH,合并结果到 self.devices,变化则通知。"""
        found = await discover_renderers(self.session)
        seen: set[str] = {d.udn for d in found}

        changed = False
        # 新增 / 续命
        for info in found:
            existing = self.devices.get(info.udn)
            if existing is None:
                self.devices[info.udn] = DlnaDevice(info=info)
                changed = True
                _LOGGER.info("发现 DLNA 设备: %s (%s)", info.name, info.udn)
            else:
                existing.missing_count = 0
                # 控制 URL 可能因设备重启而变化,刷新
                if existing.info.av_transport_url != info.av_transport_url:
                    existing.info = info
                    changed = True

        # 离线判定
        for udn, dev in list(self.devices.items()):
            if udn not in seen:
                dev.missing_count += 1
                if dev.missing_count > DEVICE_MISSING_LIMIT:
                    _LOGGER.info("DLNA 设备离线移除: %s", dev.name)
                    self.devices.pop(udn, None)
                    changed = True

        if changed:
            self._notify()
