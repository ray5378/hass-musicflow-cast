"""MusicFlow OpenSubsonic 客户端。

只用 OpenSubsonic 标准协议与 MusicFlow 交互(本项目后端原生支持):
  - 浏览:getArtists/getArtist/getAlbum/getPlaylists/getPlaylist/search3/getGenres/...
  - 流:  /rest/stream.view   (DLNA 设备直接 GET,URL 内嵌 u/t/s 鉴权)
  - 封面:/rest/getCoverArt.view

鉴权采用 OpenSubsonic 标准参数 u / t / s(token = md5(password + salt)),每次请求
重新生成 salt。这样拼出来的 URL 任何兼容 OpenSubsonic 的客户端(含 DLNA 网桥)都能
直接复用,不需要 MusicFlow 的自定义 ?token=<apiKey> 扩展。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import string
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp
from yarl import URL

from .const import (
    REQUEST_TIMEOUT,
    SUBSONIC_CLIENT,
    SUBSONIC_PREFIX,
    SUBSONIC_VERSION,
)

_LOGGER = logging.getLogger(__name__)


class MusicFlowError(Exception):
    """MusicFlow 请求失败。"""


class MusicFlowAuthError(MusicFlowError):
    """用户名/密码无效。"""


def _subsonic_token(password: str, salt: str) -> str:
    """OpenSubsonic 标准 token = md5(plaintext_password + clientSalt)。"""
    return hashlib.md5((password + salt).encode("utf-8")).hexdigest()


def _new_salt(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


class MusicFlowClient:
    """OpenSubsonic 客户端(媒体源)。"""

    def __init__(self, session: aiohttp.ClientSession, url: str, username: str, password: str, verify_ssl: bool = True) -> None:
        self._session = session
        self._base_url = url.rstrip("/")
        self._username = username
        self._password = password
        self._verify_ssl = verify_ssl

    @property
    def base_url(self) -> str:
        return self._base_url

    # ==================== 通用请求 ====================
    def _auth_params(self) -> dict[str, str]:
        salt = _new_salt()
        return {
            "u": self._username,
            "t": _subsonic_token(self._password, salt),
            "s": salt,
            "v": SUBSONIC_VERSION,
            "c": SUBSONIC_CLIENT,
        }

    def _request_kwargs(self) -> dict[str, Any]:
        return {
            "ssl": False if not self._verify_ssl else None,
            "timeout": aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        }

    async def _subsonic(self, view: str, params: dict | None = None, *, is_json: bool = True) -> dict:
        """调用 OpenSubsonic 端点,返回已剥壳的 subsonic-response。"""
        q = self._auth_params()
        q.update(params or {})
        if is_json:
            q["f"] = "json"
        try:
            async with self._session.get(
                URL(f"{self._base_url}{SUBSONIC_PREFIX}/{view}", encoded=True),
                params=q,
                **self._request_kwargs(),
            ) as resp:
                if resp.status == 401:
                    raise MusicFlowAuthError(f"{view} 认证失败(401)")
                if resp.status >= 400:
                    text = await resp.text()
                    raise MusicFlowError(f"{view} 失败 {resp.status}: {text[:200]}")
                if is_json:
                    data = await resp.json()
                else:
                    # 流/封面:调用方自行处理响应体
                    return {}
        except (TimeoutError, asyncio.TimeoutError) as err:
            raise MusicFlowError(f"{view} 超时") from err
        except aiohttp.ClientError as err:
            raise MusicFlowError(f"{view} 网络错误: {err}") from err

        resp = data.get("subsonic-response", {})
        if resp.get("status") == "failed":
            err = resp.get("error", {})
            if err.get("code") in (40, 41):
                raise MusicFlowAuthError(err.get("message", "认证失败"))
            raise MusicFlowError(f"{view}: {err.get('message', '未知错误')}")
        return resp

    # ==================== 连通性校验 ====================
    async def async_verify(self) -> dict:
        """用 OpenSubsonic ping 校验凭据(标准做法,不需要 /rest/api/* 内部端点)。"""
        try:
            await self._subsonic("ping")
        except MusicFlowAuthError:
            raise
        except MusicFlowError as err:
            raise MusicFlowError(f"无法连接 MusicFlow: {err}") from err
        return {"username": self._username}

    # ==================== 浏览 ====================
    async def async_get_artists(self) -> dict:
        return await self._subsonic("getArtists")

    async def async_get_artist(self, artist_id: str) -> dict:
        return await self._subsonic("getArtist", {"id": artist_id})

    async def async_get_album_list(self, list_type: str = "alphabeticalByName", size: int = 300) -> dict:
        return await self._subsonic("getAlbumList2", {"type": list_type, "size": size})

    async def async_get_album(self, album_id: str) -> dict:
        return await self._subsonic("getAlbum", {"id": album_id})

    async def async_get_playlists(self) -> dict:
        return await self._subsonic("getPlaylists")

    async def async_get_playlist(self, playlist_id: str) -> dict:
        return await self._subsonic("getPlaylist", {"id": playlist_id})

    async def async_get_genres(self) -> dict:
        return await self._subsonic("getGenres")

    async def async_get_songs_by_genre(self, genre: str, count: int = 300) -> dict:
        return await self._subsonic("getSongsByGenre", {"genre": genre, "count": count})

    async def async_search(self, query: str, count: int = 60) -> dict:
        return await self._subsonic(
            "search3",
            {"query": query, "artistCount": count, "albumCount": count, "songCount": count},
        )

    async def async_get_song(self, song_id: str) -> dict:
        resp = await self._subsonic("getSong", {"id": song_id})
        return resp.get("song") or {}

    # ==================== URL 构造(给 DLNA / 前端) ====================
    def cover_url(self, cover_art: str | None, size: int = 400) -> str | None:
        """封面直链(标准 OpenSubsonic getCoverArt.view + u/t/s 鉴权)。"""
        if not cover_art:
            return None
        q = self._auth_params()
        q["id"] = str(cover_art)
        q["size"] = str(size)
        return f"{self._base_url}{SUBSONIC_PREFIX}/getCoverArt.view?{urlencode(q)}"

    def stream_url(self, song_id: str) -> str:
        """歌曲直链(标准 OpenSubsonic stream.view + u/t/s 鉴权)。

        DLNA 渲染器只能做普通 HTTP GET,带不了请求头,所以凭据走 u/t/s 内嵌在
        查询串 —— 这正是 OpenSubsonic 标准,任何兼容客户端/网桥都能直接复用。
        注意不附加 f=json(返回的是裸音频字节,不是 JSON)。
        """
        q = self._auth_params()
        q["id"] = str(song_id)
        return f"{self._base_url}{SUBSONIC_PREFIX}/stream.view?{urlencode(q)}"
