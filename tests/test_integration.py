"""End-to-end functional tests for the MusicFlow Cast integration.

These run the integration's REAL code (config flow, client, coordinator,
browse, DLNA cast) against a local aiohttp mock that emulates MusicFlow's
OpenSubsonic endpoints, on top of a real HomeAssistant instance.

This is the simulation the user asked for: it drives the exact code paths a
user hits when adding the integration ("设置"/setup) and when playing media,
so any "cannot add / cannot use" defect shows up as a failing test instead of
a cryptic HA log line.
"""
from __future__ import annotations

import asyncio
import re
import socket
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from homeassistant.helpers.test_home_assistant import async_test_home_assistant


# --------------------------------------------------------------------------
# Mock OpenSubsonic server
# --------------------------------------------------------------------------
def _song() -> dict[str, Any]:
    return {
        "id": "s1",
        "title": "Song One",
        "artist": "Artist One",
        "album": "Album One",
        "duration": 200,
        "coverArt": "c1",
        "contentType": "audio/mpeg",
        "suffix": "mp3",
    }


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {"subsonic-response": {"status": "ok", **payload}}


async def _rest_handle(request: web.Request) -> web.Response:
    view = request.match_info.get("view") or request.query.get("view", "")
    if view == "ping":
        return web.json_response(_ok({}))
    if view == "getArtists":
        return web.json_response(_ok({"artists": {"index": [{"artist": [{"id": "ar1", "name": "Artist One"}]}]}}))
    if view == "getArtist":
        return web.json_response(_ok({"artist": {"name": "Artist One", "album": [{"id": "al1", "name": "Album One"}]}}))
    if view == "getAlbum":
        return web.json_response(_ok({"album": {"name": "Album One", "song": [_song()]}}))
    if view == "getAlbumList2":
        return web.json_response(_ok({"albumList2": {"album": [{"id": "al1", "name": "Album One"}]}}))
    if view == "getPlaylists":
        return web.json_response(_ok({"playlists": {"playlist": [{"id": "pl1", "name": "Playlist One"}]}}))
    if view == "getPlaylist":
        return web.json_response(_ok({"playlist": {"name": "Playlist One", "entry": [_song()]}}))
    if view == "getGenres":
        return web.json_response(_ok({"genres": {"genre": [{"value": "Rock"}, {"value": "Pop"}]}}))
    if view == "getSongsByGenre":
        return web.json_response(_ok({"songsByGenre": {"song": [_song()]}}))
    if view == "getSong":
        return web.json_response(_ok({"song": _song()}))
    if view == "search3":
        return web.json_response(_ok({"searchResult3": {"song": [_song()]}}))
    return web.json_response(_ok({}), status=200)


async def _soap_handle(request: web.Request) -> web.Response:
    body = await request.text()
    m = re.search(r"<u:(\w+)", body)
    action = m.group(1) if m else "Unknown"
    resp = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        "<s:Body>"
        f'<u:{action}Response xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        f"</u:{action}Response>"
        "</s:Body></s:Envelope>"
    )
    return web.Response(text=resp, content_type="text/xml")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
async def mock_server():
    app = web.Application()
    app.router.add_route("GET", "/rest/{view}", _rest_handle)
    app.router.add_route("POST", "/control", _soap_handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = _free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------
async def test_config_flow_validates_and_builds_entry(mock_server: str) -> None:
    """The config flow must accept valid OpenSubsonic creds and build an entry."""
    from custom_components.musicflow_cast.config_flow import MusicFlowCastConfigFlow

    async with async_test_home_assistant() as hass:
        flow = MusicFlowCastConfigFlow()
        flow.hass = hass
        flow.context = {"source": "user"}

        created: dict[str, Any] = {}

        async def fake_create_entry(title: str, data: dict[str, Any]) -> dict[str, Any]:
            created["title"] = title
            created["data"] = data
            return {"type": "create_entry", "title": title, "data": data}

        flow.async_create_entry = fake_create_entry  # type: ignore[assignment]

        result = await flow.async_step_user(
            {"url": mock_server, "username": "u", "password": "p", "verify_ssl": True}
        )
        assert result["type"] == "create_entry", f"config flow did not create entry: {result}"
        assert created["data"]["url"] == mock_server
        assert created["data"]["username"] == "u"


async def test_config_flow_rejects_bad_credentials(mock_server: str) -> None:
    """Wrong password must surface an 'invalid_auth' error, not crash the flow."""
    from custom_components.musicflow_cast.config_flow import MusicFlowCastConfigFlow

    # Reconfigure the mock to return a failed auth response for ping.
    async with async_test_home_assistant() as hass:
        flow = MusicFlowCastConfigFlow()
        flow.hass = hass
        flow.context = {"source": "user"}
        flow.async_create_entry = lambda *a, **k: {"type": "create_entry"}  # type: ignore

        # Patch the client used by the flow to force an auth error.
        from custom_components.musicflow_cast.api import MusicFlowAuthError

        async def boom(*a, **k):
            raise MusicFlowAuthError("bad")

        import custom_components.musicflow_cast.config_flow as cf

        orig = cf.MusicFlowClient.async_verify
        cf.MusicFlowClient.async_verify = staticmethod(boom)  # type: ignore
        try:
            result = await flow.async_step_user(
                {"url": mock_server, "username": "u", "password": "wrong", "verify_ssl": True}
            )
        finally:
            cf.MusicFlowClient.async_verify = orig  # type: ignore

        assert result["type"] == "form", f"expected form with error, got: {result}"
        assert result["errors"].get("base") == "invalid_auth", result


async def test_setup_entry_and_browse(mock_server: str) -> None:
    """async_setup_entry must succeed and the media browser must return the root."""
    from custom_components.musicflow_cast import async_setup_entry
    from custom_components.musicflow_cast.browse_media import async_browse_media
    from custom_components.musicflow_cast.const import (
        CONF_PASSWORD,
        CONF_URL,
        CONF_USERNAME,
        CONF_VERIFY_SSL,
        DOMAIN,
    )

    async with async_test_home_assistant() as hass:
        hass.config.enable_custom_integrations = True

        added: list[Any] = []

        async def fake_forward(entry, platforms):
            from custom_components.musicflow_cast.media_player import async_setup_entry as mp_setup

            await mp_setup(hass, entry, lambda ents: added.extend(ents))
            return True

        hass.config_entries.async_forward_entry_setups = fake_forward  # type: ignore[assignment]

        entry = SimpleNamespace(
            domain=DOMAIN,
            data={
                CONF_URL: mock_server,
                CONF_USERNAME: "u",
                CONF_PASSWORD: "p",
                CONF_VERIFY_SSL: True,
            },
            runtime_data=None,
        )
        ok = await async_setup_entry(hass, entry)  # type: ignore[arg-type]
        assert ok is True, "async_setup_entry returned False"
        assert entry.runtime_data is not None, "coordinator not stored on entry.runtime_data"

        root = await async_browse_media(entry.runtime_data.client, None, None)
        assert root is not None
        assert len(root.children) == 4, f"expected 4 root children, got {len(root.children)}"


async def test_play_album_to_device(mock_server: str) -> None:
    """Playing an album must resolve songs and cast via DLNA without error."""
    from custom_components.musicflow_cast import async_setup_entry
    from custom_components.musicflow_cast.const import (
        CONF_PASSWORD,
        CONF_URL,
        CONF_USERNAME,
        CONF_VERIFY_SSL,
        DOMAIN,
    )
    from custom_components.musicflow_cast.dlna import DlnaDevice, DlnaDeviceInfo
    from custom_components.musicflow_cast.media_player import MusicFlowCastMediaPlayer

    async with async_test_home_assistant() as hass:
        hass.config.enable_custom_integrations = True

        async def fake_forward(entry, platforms):
            from custom_components.musicflow_cast.media_player import async_setup_entry as mp_setup

            await mp_setup(hass, entry, lambda ents: None)
            return True

        hass.config_entries.async_forward_entry_setups = fake_forward  # type: ignore[assignment]

        entry = SimpleNamespace(
            domain=DOMAIN,
            data={
                CONF_URL: mock_server,
                CONF_USERNAME: "u",
                CONF_PASSWORD: "p",
                CONF_VERIFY_SSL: True,
            },
            runtime_data=None,
        )
        await async_setup_entry(hass, entry)  # type: ignore[arg-type]
        coordinator = entry.runtime_data

        info = DlnaDeviceInfo(
            udn="uuid:test",
            name="TestRenderer",
            location=mock_server,
            av_transport_url=f"{mock_server}/control",
            rendering_control_url=f"{mock_server}/control",
        )
        dev = DlnaDevice(info=info)
        coordinator.devices["uuid:test"] = dev

        player = MusicFlowCastMediaPlayer(dev, coordinator)
        player.hass = hass
        player.async_write_ha_state = lambda: None  # isolate from HA state machine

        await player.async_play_media("album", "album:al1")
        assert len(player._queue) == 1, f"queue not built from album: {player._queue}"
        assert player._queue[0]["id"] == "s1", player._queue[0]
        assert player._queue[0]["stream_url"].startswith(mock_server), player._queue[0]
