"""从 MusicFlow(OpenSubsonic)浏览曲库,并解析容器(专辑/歌单/歌手/风格)为歌曲列表。

媒体浏览器树:
    library → 艺术家 / 专辑 / 歌单 / 风格
    艺术家 → 专辑 → 歌曲
    歌单 → 歌曲
    风格 → 歌曲

play_media 拿到的是 media_id(如 album:<id>),由 resolve_songs() 解析成可播歌曲列表。
每首歌再经 client.stream_url / cover_url 拼出 DLNA 能直接 GET 的公网直链。
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.media_player.browse_media import (
    BrowseMediaSource,
    MediaClass,
    MediaType,
)

from .const import (
    BROWSE_ROOT,
    MEDIA_TYPE_ALBUM,
    MEDIA_TYPE_ALBUMS,
    MEDIA_TYPE_ARTIST,
    MEDIA_TYPE_ARTISTS,
    MEDIA_TYPE_GENRE,
    MEDIA_TYPE_GENRES,
    MEDIA_TYPE_PLAYLIST,
    MEDIA_TYPE_PLAYLISTS,
    MEDIA_TYPE_SONG,
)
from .api import MusicFlowClient


def _child_to_song(child: dict[str, Any]) -> dict[str, Any]:
    """把 OpenSubsonic Child/ID3 归一化成内部 song dict。"""
    return {
        "id": str(child.get("id")),
        "title": child.get("title") or child.get("name") or "Unknown",
        "artist": child.get("artist") or "",
        "album": child.get("album") or "",
        "duration": int(child.get("duration") or 0),
        "cover_art": child.get("coverArt"),
        "content_type": child.get("contentType") or "audio/mpeg",
        "suffix": child.get("suffix") or "mp3",
    }


def _media(
    *,
    title: str,
    media_class: str,
    media_content_id: str,
    media_content_type: str,
    can_play: bool = False,
    can_expand: bool = True,
    children: list[BrowseMediaSource] | None = None,
    thumbnail: str | None = None,
) -> BrowseMediaSource:
    return BrowseMediaSource(
        title=title,
        media_class=media_class,
        media_content_id=media_content_id,
        media_content_type=media_content_type,
        can_play=can_play,
        can_expand=can_expand,
        children=children,
        thumbnail=thumbnail,
    )


async def async_browse_media(
    client: MusicFlowClient,
    media_content_type: str | None,
    media_content_id: str | None,
) -> BrowseMediaSource:
    """构造当前层级的 BrowseMediaSource。"""
    ctype = media_content_type or BROWSE_ROOT
    cid = media_content_id or BROWSE_ROOT

    if ctype == BROWSE_ROOT:
        return _media(
            title="MusicFlow 曲库",
            media_class=MediaClass.DIRECTORY,
            media_content_id=BROWSE_ROOT,
            media_content_type=BROWSE_ROOT,
            children=[
                _media(title="艺术家", media_class=MediaClass.ARTIST, media_content_id=MEDIA_TYPE_ARTISTS, media_content_type=MEDIA_TYPE_ARTISTS),
                _media(title="专辑", media_class=MediaClass.ALBUM, media_content_id=MEDIA_TYPE_ALBUMS, media_content_type=MEDIA_TYPE_ALBUMS),
                _media(title="歌单", media_class=MediaClass.PLAYLIST, media_content_id=MEDIA_TYPE_PLAYLISTS, media_content_type=MEDIA_TYPE_PLAYLISTS),
                _media(title="风格", media_class=MediaClass.GENRE, media_content_id=MEDIA_TYPE_GENRES, media_content_type=MEDIA_TYPE_GENRES),
            ],
        )

    if ctype == MEDIA_TYPE_ARTISTS:
        resp = await client.async_get_artists()
        children = []
        for index in (resp.get("artists", {}) or {}).get("index", []):
            for artist in index.get("artist", []):
                children.append(_media(
                    title=artist.get("name", "Unknown"),
                    media_class=MediaClass.ARTIST,
                    media_content_id=f"{MEDIA_TYPE_ARTIST}:{artist['id']}",
                    media_content_type=MEDIA_TYPE_ARTIST,
                    thumbnail=client.cover_url(artist.get("coverArt")),
                ))
        return _media(title="艺术家", media_class=MediaClass.DIRECTORY, media_content_id=MEDIA_TYPE_ARTISTS, media_content_type=MEDIA_TYPE_ARTISTS, children=children)

    if ctype == MEDIA_TYPE_ARTIST:
        artist_id = cid.split(":", 1)[1]
        resp = await client.async_get_artist(artist_id)
        artist = resp.get("artist", {})
        children = []
        for album in artist.get("album", []):
            children.append(_media(
                title=album.get("name", "Unknown"),
                media_class=MediaClass.ALBUM,
                media_content_id=f"{MEDIA_TYPE_ALBUM}:{album['id']}",
                media_content_type=MEDIA_TYPE_ALBUM,
                thumbnail=client.cover_url(album.get("coverArt")),
            ))
        return _media(title=artist.get("name", "Unknown"), media_class=MediaClass.DIRECTORY, media_content_id=cid, media_content_type=MEDIA_TYPE_ARTIST, children=children)

    if ctype == MEDIA_TYPE_ALBUMS:
        resp = await client.async_get_album_list()
        children = []
        for album in resp.get("albumList2", {}).get("album", []):
            children.append(_media(
                title=album.get("name", "Unknown"),
                media_class=MediaClass.ALBUM,
                media_content_id=f"{MEDIA_TYPE_ALBUM}:{album['id']}",
                media_content_type=MEDIA_TYPE_ALBUM,
                thumbnail=client.cover_url(album.get("coverArt")),
            ))
        return _media(title="专辑", media_class=MediaClass.DIRECTORY, media_content_id=MEDIA_TYPE_ALBUMS, media_content_type=MEDIA_TYPE_ALBUMS, children=children)

    if ctype == MEDIA_TYPE_ALBUM:
        album_id = cid.split(":", 1)[1]
        return await _album_node(client, album_id)

    if ctype == MEDIA_TYPE_PLAYLISTS:
        resp = await client.async_get_playlists()
        children = []
        for pl in (resp.get("playlists", {}) or {}).get("playlist", []):
            children.append(_media(
                title=pl.get("name", "Unknown"),
                media_class=MediaClass.PLAYLIST,
                media_content_id=f"{MEDIA_TYPE_PLAYLIST}:{pl['id']}",
                media_content_type=MEDIA_TYPE_PLAYLIST,
                thumbnail=client.cover_url(pl.get("coverArt")),
            ))
        return _media(title="歌单", media_class=MediaClass.DIRECTORY, media_content_id=MEDIA_TYPE_PLAYLISTS, media_content_type=MEDIA_TYPE_PLAYLISTS, children=children)

    if ctype == MEDIA_TYPE_PLAYLIST:
        playlist_id = cid.split(":", 1)[1]
        return await _playlist_node(client, playlist_id)

    if ctype == MEDIA_TYPE_GENRES:
        resp = await client.async_get_genres()
        children = []
        for g in (resp.get("genres", {}) or {}).get("genre", []):
            name = g.get("value")
            if not name:
                continue
            children.append(_media(
                title=name,
                media_class=MediaClass.GENRE,
                media_content_id=f"{MEDIA_TYPE_GENRE}:{name}",
                media_content_type=MEDIA_TYPE_GENRE,
            ))
        return _media(title="风格", media_class=MediaClass.DIRECTORY, media_content_id=MEDIA_TYPE_GENRES, media_content_type=MEDIA_TYPE_GENRES, children=children)

    if ctype == MEDIA_TYPE_GENRE:
        genre = cid.split(":", 1)[1]
        resp = await client.async_get_songs_by_genre(genre)
        children = []
        for song in (resp.get("songsByGenre", {}) or {}).get("song", []):
            children.append(_song_node(song, client))
        return _media(title=genre, media_class=MediaClass.DIRECTORY, media_content_id=cid, media_content_type=MEDIA_TYPE_GENRE, children=children)

    # 未知 / 单首歌:返回空目录
    return _media(title="MusicFlow", media_class=MediaClass.DIRECTORY, media_content_id=BROWSE_ROOT, media_content_type=BROWSE_ROOT, children=[])


async def _album_node(client: MusicFlowClient, album_id: str) -> BrowseMediaSource:
    resp = await client.async_get_album(album_id)
    album = resp.get("album", {})
    children = [_song_node(s, client) for s in album.get("song", [])]
    return _media(
        title=album.get("name", "Unknown"),
        media_class=MediaClass.ALBUM,
        media_content_id=f"{MEDIA_TYPE_ALBUM}:{album_id}",
        media_content_type=MEDIA_TYPE_ALBUM,
        children=children,
        thumbnail=client.cover_url(album.get("coverArt")),
    )


async def _playlist_node(client: MusicFlowClient, playlist_id: str) -> BrowseMediaSource:
    resp = await client.async_get_playlist(playlist_id)
    playlist = resp.get("playlist", {})
    children = [_song_node(s, client) for s in playlist.get("entry", [])]
    return _media(
        title=playlist.get("name", "Unknown"),
        media_class=MediaClass.PLAYLIST,
        media_content_id=f"{MEDIA_TYPE_PLAYLIST}:{playlist_id}",
        media_content_type=MEDIA_TYPE_PLAYLIST,
        children=children,
        thumbnail=client.cover_url(playlist.get("coverArt")),
    )


def _song_node(song: dict[str, Any], client: MusicFlowClient) -> BrowseMediaSource:
    return _media(
        title=song.get("title", "Unknown"),
        media_class=MediaClass.TRACK,
        media_content_id=f"{MEDIA_TYPE_SONG}:{song['id']}",
        media_content_type=MEDIA_TYPE_SONG,
        can_play=True,
        can_expand=False,
        thumbnail=client.cover_url(song.get("coverArt")),
    )


async def resolve_songs(
    client: MusicFlowClient, media_type: str, media_id: str
) -> list[dict[str, Any]]:
    """把一个 media_id 解析成可播歌曲列表(song dict 列表)。

    专辑/歌单/歌手/风格 → 多首;单首歌 → 自身。
    """
    if media_type == MEDIA_TYPE_SONG:
        song = await client.async_get_song(media_id)
        return [_child_to_song(song)] if song else []

    if media_type == MEDIA_TYPE_ALBUM:
        resp = await client.async_get_album(media_id)
        return [_child_to_song(s) for s in resp.get("album", {}).get("song", [])]

    if media_type == MEDIA_TYPE_PLAYLIST:
        resp = await client.async_get_playlist(media_id)
        return [_child_to_song(s) for s in resp.get("playlist", {}).get("entry", [])]

    if media_type == MEDIA_TYPE_ARTIST:
        # 艺术家 → 所有专辑的歌曲(可能很多,但 MVP 直接铺平)
        resp = await client.async_get_artist(media_id)
        songs: list[dict[str, Any]] = []
        for album in resp.get("artist", {}).get("album", []):
            ar = await client.async_get_album(album["id"])
            songs.extend(_child_to_song(s) for s in ar.get("album", {}).get("song", []))
        return songs

    if media_type == MEDIA_TYPE_GENRE:
        resp = await client.async_get_songs_by_genre(media_id)
        return [_child_to_song(s) for s in resp.get("songsByGenre", {}).get("song", [])]

    return []
