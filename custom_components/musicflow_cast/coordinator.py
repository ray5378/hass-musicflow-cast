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
from typing import Any, Callable

import aiohttp

from .const import DEVICE_MISSING_LIMIT, DISCOVERY_INTERVAL
from .discovery import DlnaDeviceInfo, discover_renderers
from .dlna import DlnaDevice

_LOGGER = logging.getLogger(__name__)

DeviceChangedCallback = Callable[[], Any]


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
        self._started = False

    # ==================== 生命周期 ====================
    async def async_start(self) -> None:
        if self._started:
            return
        self._started = True
        self._discovery_task = self.hass.async_create_task(self._discovery_loop())
        # 首轮发现先跑一次,让实体能尽快出现;失败不致命(如网络/容器限制),
        # 后台发现循环会持续重试,绝不向 setup 冒泡。
        try:
            await self.async_discover()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("首轮 DLNA 发现失败(后台将自动重试)")

    async def async_shutdown(self) -> None:
        if self._discovery_task is not None:
            self._discovery_task.cancel()
            try:
                await self._discovery_task
            except asyncio.CancelledError:
                pass
            self._discovery_task = None
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
