"""Lightweight fake ``HomeAssistant`` so we can drive the integration's REAL code
(config flow, setup, coordinator, media_player) without the full HA runtime.

Only the surface the integration actually touches is implemented:
  - hass.data (dict, used by async_get_clientsession)
  - hass.loop (running loop, used for aiohttp session + time())
  - hass.async_create_task
  - hass.helpers.event.async_track_time_interval
  - hass.config_entries.{async_entry_for_domain_unique_id, async_forward_entry_setups, async_unload_platforms}
  - hass.config_entries.flow.{async_progress_by_handler, async_abort}
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any


class FakeHass:
    """Minimal stand-in for HomeAssistant."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.loop = asyncio.get_running_loop()

        flow_mgr = SimpleNamespace(
            async_progress_by_handler=lambda *a, **k: [],
            async_abort=lambda *a, **k: None,
        )
        self.config_entries = SimpleNamespace(
            flow=flow_mgr,
            async_entry_for_domain_unique_id=lambda *a, **k: None,
            async_forward_entry_setups=self._forward_entry_setups,
            async_unload_platforms=lambda entry, platforms: True,
        )
        self.helpers = SimpleNamespace(
            event=SimpleNamespace(
                async_track_time_interval=lambda cb, interval: (lambda: None)
            )
        )

    async def _forward_entry_setups(self, entry: Any, platforms: Any) -> bool:
        from musicflow_cast.media_player import async_setup_entry as mp_setup

        await mp_setup(self, entry, lambda ents: None)
        return True

    def async_create_task(self, coro: Any) -> Any:
        return asyncio.create_task(coro)


async def close_hass(hass: FakeHass) -> None:
    """Close any aiohttp sessions HA's async_get_clientsession created."""
    for key in ("aiohttp_session", "aiohttp_session_ssl"):
        session = hass.data.get(key)
        if session is not None:
            await session.close()
