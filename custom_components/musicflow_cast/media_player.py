"""media_player 实体:每台发现的本地 DLNA 设备对应一个实体。

播放流程(对齐 MusicFlow 后端 control.ts 的 castToDevice):
  1. 从 MusicFlow 曲库挑歌(经 OpenSubsonic 浏览 / 媒体浏览器)→ 解析成歌曲列表
  2. 给设备 SetAVTransportURI(内嵌公网直链 stream.view?u/t/s + DIDL 元数据)
  3. Play —— 设备自己回连 MusicFlow 把音频拉走(跨网场景的关键)
  4. 播完(状态变 STOPPED)自动下一首;队列空则停

状态轮询:每个实体自带定时器,周期性 GetTransportInfo/GetPositionInfo/GetVolume/GetMute
回填 HA 属性。MusicFlow 在这里只当媒体源,不参与 DLNA 控制。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .browse_media import async_browse_media, resolve_songs
from .const import (
    DOMAIN,
    MEDIA_TYPE_SONG,
    POLL_INTERVAL,
)
from .coordinator import MusicFlowCastCoordinator
from .dlna import DlnaDevice
from .api import MusicFlowClient

_LOGGER = logging.getLogger(__name__)

# OpenSubsonic contentType → DIDL mime(直传给 DLNA 设备)
_MIME_FALLBACK = {
    "mp3": "audio/mpeg", "flac": "audio/flac", "wav": "audio/wav",
    "aac": "audio/aac", "ogg": "audio/ogg", "m4a": "audio/mp4",
    "wma": "audio/x-ms-wma", "ape": "audio/ape", "aiff": "audio/aiff",
    "opus": "audio/opus",
}


def _mime_for(content_type: str | None, suffix: str | None) -> str:
    if content_type:
        return content_type
    return _MIME_FALLBACK.get((suffix or "").lower(), "audio/mpeg")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: MusicFlowCastCoordinator = hass.data[DOMAIN][entry.entry_id]
    manager = _DeviceManager(hass, coordinator, async_add_entities)
    coordinator.async_on_devices_changed(manager.reconcile)
    await manager.reconcile()


class _DeviceManager:
    """按协调器设备注册表增删 media_player 实体。"""

    def __init__(self, hass: HomeAssistant, coordinator: MusicFlowCastCoordinator, async_add_entities: AddEntitiesCallback) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.async_add_entities = async_add_entities
        self._entities: dict[str, MusicFlowCastMediaPlayer] = {}

    async def reconcile(self) -> None:
        # 新增
        for udn, device in self.coordinator.devices.items():
            if udn not in self._entities:
                entity = MusicFlowCastMediaPlayer(device, self.coordinator)
                self._entities[udn] = entity
                self.async_add_entities([entity])
        # 移除(离线超过阈值)
        for udn in list(self._entities):
            if udn not in self.coordinator.devices:
                entity = self._entities.pop(udn)
                self.hass.async_create_task(entity.async_remove(force_remove=True))


class MusicFlowCastMediaPlayer(MediaPlayerEntity):
    """一个本地 DLNA 渲染器。"""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, device: DlnaDevice, coordinator: MusicFlowCastCoordinator) -> None:
        self._device = device
        self.coordinator = coordinator
        self._client: MusicFlowClient = coordinator.client
        self._queue: list[dict[str, Any]] = []
        self._index = 0
        self._playing = False
        self._last_cast_at = 0.0
        self._unsub_poll = None
        self._attr_unique_id = device.udn
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.udn)},
            "name": device.name,
            "manufacturer": "DLNA",
            "model": "MediaRenderer",
        }

    # ==================== 状态 ====================
    @property
    def available(self) -> bool:
        return self._device.state.available

    @property
    def state(self) -> MediaPlayerState | None:
        st = self._device.state.transport_state
        if st == "PLAYING" or st == "TRANSITIONING":
            return MediaPlayerState.PLAYING
        if st == "PAUSED_PLAYBACK":
            return MediaPlayerState.PAUSED
        return MediaPlayerState.IDLE

    @property
    def volume_level(self) -> float | None:
        return self._device.state.volume / 100.0

    @property
    def is_volume_muted(self) -> bool | None:
        return self._device.state.muted

    @property
    def media_title(self) -> str | None:
        return self._device.state.title

    @property
    def media_artist(self) -> str | None:
        return self._device.state.artist

    @property
    def media_album_name(self) -> str | None:
        return self._device.state.album

    @property
    def media_content_id(self) -> str | None:
        if self._queue and 0 <= self._index < len(self._queue):
            return self._queue[self._index].get("id")
        return None

    @property
    def media_duration(self) -> float | None:
        return self._device.state.duration or None

    @property
    def media_position(self) -> float | None:
        return self._device.state.position or None

    @property
    def media_image_url(self) -> str | None:
        return self._device.state.cover_url

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        return (
            MediaPlayerEntityFeature.PLAY
            | MediaPlayerEntityFeature.PAUSE
            | MediaPlayerEntityFeature.STOP
            | MediaPlayerEntityFeature.SEEK
            | MediaPlayerEntityFeature.VOLUME_SET
            | MediaPlayerEntityFeature.VOLUME_MUTE
            | MediaPlayerEntityFeature.BROWSE_MEDIA
            | MediaPlayerEntityFeature.PLAY_MEDIA
            | MediaPlayerEntityFeature.NEXT_TRACK
            | MediaPlayerEntityFeature.PREVIOUS_TRACK
        )

    # ==================== 生命周期 ====================
    async def async_added_to_hass(self) -> None:
        self._unsub_poll = self.hass.helpers.event.async_track_time_interval(
            self._async_poll, asyncio.timedelta(seconds=POLL_INTERVAL)
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_poll is not None:
            self._unsub_poll()
            self._unsub_poll = None

    async def _async_poll(self, _now=None) -> None:
        try:
            await self._device.async_update(self.coordinator.session)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("轮询设备 %s 状态失败", self._device.udn)
        # 自动下一首:正在播放且自然结束(STOPPED 且距上次投屏 > 3s 防抖)
        if self._playing and self._device.state.transport_state == "STOPPED":
            if (self.hass.loop.time() - self._last_cast_at) > 3:
                await self._advance()
        self.async_write_ha_state()

    # ==================== 播放控制 ====================
    async def async_play_media(self, media_type: str, media_id: str, **kwargs: Any) -> None:
        client = self._client
        if media_type == MEDIA_TYPE_SONG:
            songs = await client.async_get_song(media_id)
            songs = [songs] if songs else []
        else:
            songs = await resolve_songs(client, media_type, media_id)
        if not songs:
            _LOGGER.warning("无法解析播放目标 %s:%s", media_type, media_id)
            return

        self._queue = []
        for s in songs:
            self._queue.append({
                "id": str(s.get("id")),
                "title": s.get("title") or "Unknown",
                "artist": s.get("artist") or "",
                "album": s.get("album") or "",
                "duration": int(s.get("duration") or 0),
                "stream_url": client.stream_url(str(s.get("id"))),
                "cover_url": client.cover_url(s.get("cover_art")),
                "mime": _mime_for(s.get("content_type"), s.get("suffix")),
            })
        self._index = 0
        self._playing = True
        await self._cast_current()

    async def _cast_current(self) -> None:
        if not self._queue or self._index >= len(self._queue):
            return
        song = self._queue[self._index]
        device = self._device
        session = self.coordinator.session
        # 若正在播放/暂停,先停(对齐 control.ts 的 Stop→SetURI→Play 顺序)
        if device.state.transport_state in ("PLAYING", "PAUSED_PLAYBACK"):
            await device.async_stop(session)
            await asyncio.sleep(0.3)
        ok = await device.async_set_uri(session, song["stream_url"], {
            "title": song["title"],
            "artist": song["artist"],
            "album": song["album"],
            "cover_url": song["cover_url"],
            "mime": song["mime"],
            "duration": song["duration"],
        })
        if not ok:
            _LOGGER.warning("DLNA %s SetAVTransportURI 失败", device.udn)
            return
        await device.async_play(session)
        self._last_cast_at = self.hass.loop.time()
        self.async_write_ha_state()

    async def _advance(self, step: int = 1) -> None:
        self._index += step
        if 0 <= self._index < len(self._queue):
            await self._cast_current()
        else:
            self._playing = False
            await self._device.async_stop(self.coordinator.session)
            self.async_write_ha_state()

    async def async_media_play(self) -> None:
        if self._queue:
            self._playing = True
            await self._device.async_play(self.coordinator.session)

    async def async_media_pause(self) -> None:
        await self._device.async_pause(self.coordinator.session)

    async def async_media_stop(self) -> None:
        self._playing = False
        await self._device.async_stop(self.coordinator.session)
        self.async_write_ha_state()

    async def async_media_next_track(self) -> None:
        await self._advance(1)

    async def async_media_previous_track(self) -> None:
        if self._index > 0:
            await self._advance(-1)

    async def async_media_seek(self, position: float) -> None:
        await self._device.async_seek(self.coordinator.session, position)

    async def async_set_volume_level(self, volume: float) -> None:
        await self._device.async_set_volume(self.coordinator.session, int(volume * 100))
        self.async_write_ha_state()

    async def async_mute_volume(self, mute: bool) -> None:
        await self._device.async_set_mute(self.coordinator.session, mute)
        self.async_write_ha_state()

    # ==================== 媒体浏览器 ====================
    async def async_browse_media(self, media_content_type: str | None = None, media_content_id: str | None = None):
        return await async_browse_media(self._client, media_content_type, media_content_id)
