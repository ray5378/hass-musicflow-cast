"""Functional simulation of the MusicFlow Cast integration.

This is the "simulation test" the user asked for: it drives the integration's
REAL code paths -- config flow, async_setup_entry, media browser, song
resolution, DLNA cast -- against a local aiohttp mock that emulates MusicFlow's
OpenSubsonic endpoints and a DLNA device's UPnP/SOAP control endpoint.

Any "cannot add / cannot use" defect shows up here as a failing test instead of
a cryptic HA log line. No full Home Assistant runtime is required; a lightweight
`FakeHass` (see helpers.py) provides exactly the surface the integration uses.
"""
from __future__ import annotations

import re
import socket
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from helpers import FakeHass, close_hass

from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryNotReady


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
    return web.json_response(_ok({}))


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


@pytest.fixture
async def patched_session(monkeypatch):
    """Return a real aiohttp session for the integration's `async_get_clientsession`
    calls, bypassing HA's session factory (which reads hass.data['network'] that a
    FakeHass cannot provide). Keeps tests focused on integration logic."""
    session = aiohttp.ClientSession()

    def fake(hass, verify_ssl=True):
        return session

    # __init__.async_setup_entry imports the function INSIDE the function body
    monkeypatch.setattr("homeassistant.helpers.aiohttp_client.async_get_clientsession", fake)
    # config_flow imports it at module level
    import musicflow_cast.config_flow as cf

    monkeypatch.setattr(cf, "async_get_clientsession", fake)
    yield session
    await session.close()


# --------------------------------------------------------------------------
# Config flow
# --------------------------------------------------------------------------
async def test_config_flow_success_builds_entry(mock_server: str, patched_session) -> None:
    """Valid OpenSubsonic creds must create an entry (the "add" happy path)."""
    from musicflow_cast.config_flow import MusicFlowCastConfigFlow
    from musicflow_cast.const import (
        CONF_PASSWORD,
        CONF_URL,
        CONF_USERNAME,
        CONF_VERIFY_SSL,
        DOMAIN,
    )

    hass = FakeHass()
    try:
        flow = MusicFlowCastConfigFlow()
        flow.hass = hass
        flow.context = {"source": "user"}
        flow.flow_id = "test"
        flow.handler = DOMAIN

        result = await flow.async_step_user(
            {CONF_URL: mock_server, CONF_USERNAME: "u", CONF_PASSWORD: "p", CONF_VERIFY_SSL: True}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY, f"flow did not create entry: {result}"
        assert result["data"][CONF_URL] == mock_server
        assert result["data"][CONF_USERNAME] == "u"
    finally:
        await close_hass(hass)


async def test_config_flow_invalid_auth(mock_server: str, monkeypatch, patched_session) -> None:
    """Wrong password must surface 'invalid_auth', not crash the flow."""
    from musicflow_cast.config_flow import MusicFlowCastConfigFlow
    from musicflow_cast.const import (
        CONF_PASSWORD,
        CONF_URL,
        CONF_USERNAME,
        CONF_VERIFY_SSL,
        DOMAIN,
    )
    from musicflow_cast.api import MusicFlowAuthError

    async def boom(*a, **k):
        raise MusicFlowAuthError("bad")

    monkeypatch.setattr("musicflow_cast.api.MusicFlowClient.async_verify", boom)

    hass = FakeHass()
    try:
        flow = MusicFlowCastConfigFlow()
        flow.hass = hass
        flow.context = {"source": "user"}
        flow.flow_id = "test"
        flow.handler = DOMAIN

        result = await flow.async_step_user(
            {CONF_URL: mock_server, CONF_USERNAME: "u", CONF_PASSWORD: "wrong", CONF_VERIFY_SSL: True}
        )
        assert result["type"] == FlowResultType.FORM, f"expected form, got: {result}"
        assert result["errors"].get("base") == "invalid_auth", result
    finally:
        await close_hass(hass)


async def test_config_flow_cannot_connect(mock_server: str, monkeypatch, patched_session) -> None:
    """Unreachable server must surface 'cannot_connect'."""
    from musicflow_cast.config_flow import MusicFlowCastConfigFlow
    from musicflow_cast.const import (
        CONF_PASSWORD,
        CONF_URL,
        CONF_USERNAME,
        CONF_VERIFY_SSL,
        DOMAIN,
    )
    from musicflow_cast.api import MusicFlowError

    async def boom(*a, **k):
        raise MusicFlowError("down")

    monkeypatch.setattr("musicflow_cast.api.MusicFlowClient.async_verify", boom)

    hass = FakeHass()
    try:
        flow = MusicFlowCastConfigFlow()
        flow.hass = hass
        flow.context = {"source": "user"}
        flow.flow_id = "test"
        flow.handler = DOMAIN

        result = await flow.async_step_user(
            {CONF_URL: mock_server, CONF_USERNAME: "u", CONF_PASSWORD: "p", CONF_VERIFY_SSL: True}
        )
        assert result["type"] == FlowResultType.FORM, f"expected form, got: {result}"
        assert result["errors"].get("base") == "cannot_connect", result
    finally:
        await close_hass(hass)


async def test_config_flow_ssl_error(mock_server: str, monkeypatch, patched_session) -> None:
    """SSL failure must surface 'ssl_error'."""
    from musicflow_cast.config_flow import MusicFlowCastConfigFlow
    from musicflow_cast.const import (
        CONF_PASSWORD,
        CONF_URL,
        CONF_USERNAME,
        CONF_VERIFY_SSL,
        DOMAIN,
    )
    from musicflow_cast.api import MusicFlowSSLError

    async def boom(*a, **k):
        raise MusicFlowSSLError("cert")

    monkeypatch.setattr("musicflow_cast.api.MusicFlowClient.async_verify", boom)

    hass = FakeHass()
    try:
        flow = MusicFlowCastConfigFlow()
        flow.hass = hass
        flow.context = {"source": "user"}
        flow.flow_id = "test"
        flow.handler = DOMAIN

        result = await flow.async_step_user(
            {CONF_URL: mock_server, CONF_USERNAME: "u", CONF_PASSWORD: "p", CONF_VERIFY_SSL: True}
        )
        assert result["type"] == FlowResultType.FORM, f"expected form, got: {result}"
        assert result["errors"].get("base") == "ssl_error", result
    finally:
        await close_hass(hass)


# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------
def _make_entry(url: str):
    from musicflow_cast.const import (
        CONF_PASSWORD,
        CONF_URL,
        CONF_USERNAME,
        CONF_VERIFY_SSL,
        DOMAIN,
    )

    return SimpleNamespace(
        domain=DOMAIN,
        data={
            CONF_URL: url,
            CONF_USERNAME: "u",
            CONF_PASSWORD: "p",
            CONF_VERIFY_SSL: True,
        },
        runtime_data=None,
    )


async def test_setup_success_and_browse(mock_server: str, patched_session) -> None:
    """async_setup_entry must succeed and the media browser must return the root."""
    from musicflow_cast import async_setup_entry
    from musicflow_cast.browse_media import async_browse_media

    hass = FakeHass()
    try:
        entry = _make_entry(mock_server)
        ok = await async_setup_entry(hass, entry)  # type: ignore[arg-type]
        assert ok is True, "async_setup_entry returned False"
        assert entry.runtime_data is not None, "coordinator not stored on entry.runtime_data"

        root = await async_browse_media(entry.runtime_data.client, None, None)
        assert root is not None
        assert len(root.children) == 4, f"expected 4 root children, got {len(root.children)}"
    finally:
        if entry.runtime_data is not None:
            await entry.runtime_data.async_shutdown()
        await close_hass(hass)


async def test_setup_unreachable_is_retryable(mock_server: str, monkeypatch, patched_session) -> None:
    """An unreachable server must raise ConfigEntryNotReady (retry), not hard-crash."""
    from musicflow_cast import async_setup_entry
    from musicflow_cast.api import MusicFlowError

    async def boom(*a, **k):
        raise MusicFlowError("down")

    monkeypatch.setattr("musicflow_cast.api.MusicFlowClient.async_verify", boom)

    hass = FakeHass()
    try:
        entry = _make_entry(mock_server)
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry)  # type: ignore[arg-type]
    finally:
        await close_hass(hass)


# --------------------------------------------------------------------------
# Play (cast) to a DLNA device
# --------------------------------------------------------------------------
async def test_play_album_to_device(mock_server: str, patched_session) -> None:
    """Playing an album must resolve songs and cast via DLNA without error."""
    from musicflow_cast import async_setup_entry
    from musicflow_cast.dlna import DlnaDevice, DlnaDeviceInfo
    from musicflow_cast.media_player import MusicFlowCastMediaPlayer

    hass = FakeHass()
    try:
        entry = _make_entry(mock_server)
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
        # device should have been told to play
        assert player._playing is True
    finally:
        if entry.runtime_data is not None:
            await entry.runtime_data.async_shutdown()
        await close_hass(hass)


# --------------------------------------------------------------------------
# Entity creation: discovered DLNA device -> media_player entity
# --------------------------------------------------------------------------
async def test_device_discovery_creates_entity(mock_server: str, patched_session) -> None:
    """When the coordinator discovers a DLNA device, a media_player entity must be
    created via reconcile -> async_add_entities. This is the exact "connect but no
    entity appears" path the user reported -- if this test fails the bug is in the
    integration; if it passes, the absence of entities means discovery found nothing
    (network/container), not a code defect."""
    from musicflow_cast import async_setup_entry
    from musicflow_cast.dlna import DlnaDevice, DlnaDeviceInfo
    from musicflow_cast.media_player import async_setup_entry as mp_setup

    hass = FakeHass()
    try:
        entry = _make_entry(mock_server)
        await async_setup_entry(hass, entry)  # type: ignore[arg-type]
        coordinator = entry.runtime_data
        assert coordinator.devices == {}, "no devices should be discovered in this sim yet"

        added: list[Any] = []
        # register the platform (registers manager.reconcile as the devices-changed callback)
        await mp_setup(hass, entry, lambda ents: added.extend(ents))

        # simulate the coordinator discovering a renderer
        info = DlnaDeviceInfo(
            udn="uuid:test",
            name="TestRenderer",
            location=mock_server,
            av_transport_url=f"{mock_server}/control",
            rendering_control_url=f"{mock_server}/control",
        )
        coordinator.devices["uuid:test"] = DlnaDevice(info=info)
        coordinator._notify()  # devices-changed -> manager.reconcile() -> async_add_entities

        assert len(added) == 1, f"expected 1 entity to be added, got {len(added)}: {added}"
        entity = added[0]
        assert entity.unique_id == "uuid:test"
        assert entity.name == "TestRenderer"

        # a second reconcile must NOT duplicate the entity
        coordinator._notify()
        assert len(added) == 1, f"entity duplicated on re-notify: {added}"
    finally:
        if entry.runtime_data is not None:
            await entry.runtime_data.async_shutdown()
        await close_hass(hass)



async def test_dlna_update_runs(mock_server: str) -> None:
    """Device status polling must run without error and mark the device available."""
    import aiohttp
    from musicflow_cast.dlna import DlnaDevice, DlnaDeviceInfo

    hass = FakeHass()
    session = aiohttp.ClientSession()
    try:
        info = DlnaDeviceInfo(
            udn="uuid:test",
            name="TestRenderer",
            location=mock_server,
            av_transport_url=f"{mock_server}/control",
            rendering_control_url=f"{mock_server}/control",
        )
        dev = DlnaDevice(info=info)
        await dev.async_update(session)
        assert dev.state.available is True
        assert dev.state.transport_state == "STOPPED"
    finally:
        await session.close()
        await close_hass(hass)
