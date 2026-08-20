"""MusicFlow Cast 集成入口。

HA 当「客户端节点」:
  - MusicFlow(反代 HTTPS)经 OpenSubsonic 只作媒体源(浏览曲库 / 拼流地址)
  - DLNA 设备在 HA 本机 LAN 发现,直接 UPnP 控制,与 MusicFlow 服务端无关

本集成不连 MusicFlow 的 WebSocket,也不反射任何服务端状态。
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .api import MusicFlowClient
from .const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL, DOMAIN
from .coordinator import MusicFlowCastCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.MEDIA_PLAYER]

# 纯配置流集成(hassfest 要求声明 CONFIG_SCHEMA)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """建立 client + coordinator,首轮发现后加载 media_player 平台。"""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    verify_ssl = entry.data.get(CONF_VERIFY_SSL, True)
    session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    client = MusicFlowClient(
        session,
        entry.data[CONF_URL],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        verify_ssl,
    )
    coordinator = MusicFlowCastCoordinator(session, client)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # 首轮发现(可能抛网络异常,交由 HA 重试 / 重认证)
    await coordinator.async_start()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: MusicFlowCastCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
