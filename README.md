# MusicFlow Cast

把 Home Assistant 当成一个 **客户端节点**:在 HA 所在的局域网发现 DLNA 渲染器,
然后把它当成 **OpenSubsonic 媒体源** 的远程 MusicFlow 投屏播放。

适用于这种场景:你的 MusicFlow 通过反代暴露成公网 HTTPS 地址(可能在另一个网络 /
另一个局域网),而你想在 **HA 这边的 DLNA 音箱/电视** 上播放它的曲库。

## 它解决了什么

现有的 `hass-musicflow` 集成是「反射器」:它只把 MusicFlow **服务端**发现的 DLNA
peer 映射成 HA 实体,自己不跑 SSDP/UPnP。当 DLNA 设备位于 HA 的局域网、而
MusicFlow 在另一个网络时,服务端永远发现不了这些设备。

本集成把控制流倒置:

- **MusicFlow** = 纯媒体源:只提供曲库浏览 + 公网直链(`OpenSubsonic` 标准协议)
- **HA** = 客户端节点:在本机 LAN 跑 SSDP 发现 DLNA,直接发 UPnP SOAP 控制设备

## 工作原理

1. HA 在本机局域网发 `M-SEARCH`(`urn:schemas-upnp-org:device:MediaRenderer:1`),
   发现 DLNA 设备 → 每台设备一个 `media_player` 实体。
2. 从 MusicFlow 的 OpenSubsonic 接口浏览曲库(艺术家 / 专辑 / 歌单 / 风格)。
3. 选中歌曲 → 拼出 **OpenSubsonic 标准流地址**
   `https://<域名>/rest/stream.view?id=<songId>&u=<user>&t=<md5(pass+salt)>&s=<salt>&v=1.16.1&c=...`
   → 直接给本地 DLNA 发 `SetAVTransportURI` + `Play`。
4. DLNA 设备自己回连 MusicFlow 把音频拉走(只要设备能访问你的反代域名即可跨网播放)。
5. 播完(状态变 `STOPPED`)自动放下一首。

> 流与浏览**全部走 OpenSubsonic 标准协议**,本项目后端原生支持,不需要任何
> MusicFlow 自定义扩展。任何兼容 OpenSubsonic 的客户端 / 网桥都能直接复用这些 URL。

## 安装(HACS)

1. HACS → 集成 → 右上角菜单 → 自定义仓库,填入本仓库 URL,类别选「集成」。
2. 搜索并安装 **MusicFlow Cast**,重启 HA。
3. 设置 → 设备与服务 → 添加集成 → MusicFlow Cast。

## 配置

| 字段 | 说明 |
| --- | --- |
| MusicFlow 地址 | 反代后的公网 HTTPS 地址,如 `https://music.example.com` |
| 用户名 | MusicFlow 登录用户名(OpenSubsonic 标准凭据) |
| 密码 | MusicFlow 登录密码(用于计算 `t=md5(password+salt)`) |
| 验证 SSL 证书 | 自签证书请关闭 |

## 前提条件

- 你的 DLNA 设备必须能访问公网(至少够得到你的反代域名),否则拉不到流 ——
  这是跨网投屏场景的固有前提。
- HA 主机与 DLNA 设备在同一局域网,且 SSDP 多播可达(M-SEARCH 能收到响应)。

## 与 `hass-musicflow` 的区别

| | hass-musicflow | hass-musicflow-cast |
| --- | --- | --- |
| 发现位置 | MusicFlow 服务端局域网 | HA 本机局域网 |
| 控制方式 | 回传 MusicFlow 服务端控制 peer | HA 直接 UPnP 控制本地设备 |
| MusicFlow 角色 | 服务端 + 媒体源 | 纯媒体源(OpenSubsonic) |
| 跨网播放 | 设备须能直连 MusicFlow | 同左(流走公网直链) |

两者互不干扰,可同时安装。
