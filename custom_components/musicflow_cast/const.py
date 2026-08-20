"""MusicFlow Cast 集成常量。

所有与 MusicFlow 的交互都走 **OpenSubsonic 标准协议**(本项目后端原生支持,
见 backend/src/middleware/auth.ts 的 authenticateOpenSubsonic):
    GET /rest/stream.view?id=..&u=<user>&t=<md5(pass+salt)>&s=<salt>&v=1.16.1&c=..
鉴权参数 u / t / s 直接挂在查询串上 —— DLNA 渲染器只会做一次普通 HTTP GET 把
字节拉走,带不了任何请求头,所以凭据必须内嵌在 URL 里。这正是 OpenSubsonic 的
标准做法,任何兼容客户端/网桥都能直接复用,不需要 MusicFlow 自定义扩展。
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "musicflow_cast"

# ==================== ConfigEntry 字段 ====================
# 标准 OpenSubsonic 凭据(用户名 + 密码),不是 MusicFlow 专用 API Key。
# 用密码是因为标准 u/t/s token = md5(password + salt),API Key 算不出来。
CONF_URL: Final = "url"          # MusicFlow 反代后的公网 HTTPS 地址
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_VERIFY_SSL: Final = "verify_ssl"

DEFAULT_PORT: Final = 46400

# ==================== 后端路径(OpenSubsonic) ====================
SUBSONIC_PREFIX: Final = "/rest"   # 浏览 / 流 / 封面都挂在这里
WS_PATH: Final = "/ws"             # 本集成不连 WS(只做媒体源),保留常量以备扩展

# ==================== OpenSubsonic 客户端标识 ====================
SUBSONIC_VERSION: Final = "1.16.1"
SUBSONIC_CLIENT: Final = "hass_musicflow_cast"

# ==================== 运行参数 ====================
REQUEST_TIMEOUT: Final = 20
# SSDP 发现周期:主动 M-SEARCH 一轮,收集局域网内 MediaRenderer
DISCOVERY_INTERVAL: Final = 60
# 设备状态轮询:GetTransportInfo/GetPositionInfo/GetVolume/GetMute
POLL_INTERVAL: Final = 5
# 设备连续 N 次发现不到即判定离线(移除实体)
DEVICE_MISSING_LIMIT: Final = 3

# ==================== 媒体 URI ====================
BROWSE_ROOT: Final = "library"

# ==================== 浏览媒体内容类型 ====================
MEDIA_TYPE_LIBRARY: Final = "library"
MEDIA_TYPE_ARTISTS: Final = "artists"
MEDIA_TYPE_ARTIST: Final = "artist"
MEDIA_TYPE_ALBUMS: Final = "albums"
MEDIA_TYPE_ALBUM: Final = "album"
MEDIA_TYPE_PLAYLISTS: Final = "playlists"
MEDIA_TYPE_PLAYLIST: Final = "playlist"
MEDIA_TYPE_GENRES: Final = "genres"
MEDIA_TYPE_GENRE: Final = "genre"
MEDIA_TYPE_SONG: Final = "song"

# 可作为播放目标的容器(解析成歌曲列表)
PLAYABLE_CONTAINERS = (MEDIA_TYPE_ALBUM, MEDIA_TYPE_PLAYLIST, MEDIA_TYPE_ARTIST, MEDIA_TYPE_GENRE)
