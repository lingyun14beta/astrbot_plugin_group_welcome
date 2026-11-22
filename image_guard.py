"""欢迎图片下载安全校验。

插件本身不下载图片（AstrBot / OneBot 客户端负责），但「把 URL 交给下载方」这一步
仍然需要防护，否则被篡改的配置可以指向内网服务，形成 SSRF：

- 只允许 ``http(s)``，拒绝带账号密码的 URL；
- 解析域名并校验所有解析结果，拒绝环回、内网、链路本地、CGNAT、保留地址
  （含云厂商元数据地址 169.254.169.254 / 100.100.100.200）；
- 跟随最多两次重定向，每一跳都重新校验，返回最终 URL；
- HEAD 探测内容类型与大小，超过上限或不是图片时拒绝；
- 本地路径只做存在性、文件类型与体积检查。

DNS 解析失败按拒绝处理（fail closed）；HEAD 探测失败（超时 / 405）不阻断，
因为部分图床不支持 HEAD，但此时 DNS / IP 校验已经通过。

注意：真实下载由 AstrBot 或 OneBot 客户端完成，本模块只能做发送前的预检，
无法防御 DNS rebinding 或下载阶段的二次重定向。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from astrbot.api import logger

_BLOCKED_NETWORKS = [
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",  # CGNAT，含部分云厂商元数据地址
        "127.0.0.0/8",
        "169.254.0.0/16",  # 链路本地，含 169.254.169.254
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
]

_ALLOWED_SCHEMES = ("http", "https")
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_MAX_REDIRECTS = 2
_PROBE_TIMEOUT = 4.0
_CACHE_TTL = 600.0
_cache: dict[str, tuple[float, bool, str, str]] = {}


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """判断 IP 是否属于禁止访问的地址段。

    Args:
        ip: 已解析的 IP 地址。

    Returns:
        内网、环回、链路本地、CGNAT、保留地址返回 True。
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True
    return any(
        ip in network for network in _BLOCKED_NETWORKS if network.version == ip.version
    )


async def _resolve_host(host: str, port: int) -> tuple[bool, str]:
    """解析域名并校验所有解析结果。

    Args:
        host: 主机名或 IP 字面量。
        port: 端口。

    Returns:
        (是否安全, 失败原因)。
    """
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if _is_blocked_ip(literal):
            return False, f"目标地址 {host} 属于内网/保留地址"
        return True, ""

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except Exception as exc:
        return False, f"域名解析失败: {exc}"

    if not infos:
        return False, "域名没有可用解析结果"

    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False, f"无法识别的解析结果: {address}"
        if _is_blocked_ip(ip):
            return False, f"域名 {host} 解析到内网/保留地址 {address}"
    return True, ""


def _is_local_source(source: str) -> bool:
    """判断配置项是否为本地路径。

    Args:
        source: 图片配置项。

    Returns:
        非 http(s) 时视为本地路径。
    """
    return not source.lower().startswith(_ALLOWED_SCHEMES)


def _check_local(source: str, max_bytes: int) -> tuple[bool, str]:
    """校验本地图片文件。

    Args:
        source: 本地路径或 ``file:///`` 形式。
        max_bytes: 允许的最大字节数。

    Returns:
        (是否可用, 失败原因)。
    """
    raw = source[8:] if source.lower().startswith("file:///") else source
    path = Path(raw)
    try:
        if not path.is_file():
            return False, "本地图片不存在或不是文件"
        size = path.stat().st_size
    except OSError as exc:
        return False, f"读取本地图片失败: {exc}"
    if size <= 0:
        return False, "本地图片为空"
    if size > max_bytes:
        return False, f"本地图片 {size} 字节，超过上限 {max_bytes}"
    return True, ""


async def _probe(url: str, max_bytes: int) -> tuple[bool, str, str]:
    """对远程图片做 HEAD 探测，并处理有限次数的重定向。

    Args:
        url: 待探测的 URL。
        max_bytes: 允许的最大字节数。

    Returns:
        (是否可用, 失败原因, 最终 URL)。
    """
    try:
        import httpx
    except ImportError:  # pragma: no cover - AstrBot 环境自带 httpx
        return True, "", url

    current = url
    try:
        async with httpx.AsyncClient(
            timeout=_PROBE_TIMEOUT,
            follow_redirects=False,
        ) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                resp = await client.head(current)
                if resp.status_code in _REDIRECT_STATUSES:
                    location = resp.headers.get("location")
                    if not location:
                        return False, "重定向缺少 Location", current
                    current = urljoin(current, location)
                    ok, reason = await _check_url_target(current)
                    if not ok:
                        return False, f"重定向目标不安全: {reason}", current
                    continue
                if resp.status_code in (405, 501):
                    return True, "", current
                if resp.status_code >= 400:
                    return False, f"HTTP {resp.status_code}", current
                content_type = (resp.headers.get("content-type") or "").lower()
                if content_type and not content_type.startswith("image/"):
                    return False, f"内容类型 {content_type} 不是图片", current
                length = resp.headers.get("content-length")
                if length and length.isdigit() and int(length) > max_bytes:
                    return False, f"图片 {length} 字节，超过上限 {max_bytes}", current
                return True, "", current
            return False, "重定向次数过多", current
    except Exception as exc:
        # 部分图床不支持 HEAD，探测失败不阻断，DNS / IP 校验已经通过
        logger.debug(f"[group_welcome] 图片探测失败，跳过体积/类型检查: {exc}")
        return True, "", current


async def _check_url_target(url: str) -> tuple[bool, str]:
    """解析并校验一个 http(s) URL 的目标地址。

    Args:
        url: 待校验 URL。

    Returns:
        (是否安全, 失败原因)。
    """
    try:
        parts = urlsplit(url)
    except Exception as exc:
        return False, f"URL 解析失败: {exc}"
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return False, f"不支持的协议 {parts.scheme}"
    if not parts.hostname:
        return False, "URL 缺少主机名"
    if "@" in parts.netloc:
        return False, "URL 不允许携带账号密码"
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError:
        return False, "URL 端口非法"
    return await _resolve_host(parts.hostname, port)


async def check_image_source(
    source: str,
    *,
    max_bytes: int,
    enabled: bool = True,
) -> tuple[bool, str, str]:
    """校验欢迎图片配置项，返回可安全使用的地址。

    Args:
        source: 图片 URL 或本地路径。
        max_bytes: 允许的最大字节数。
        enabled: 是否启用校验；关闭时原样放行。

    Returns:
        (是否可用, 失败原因, 最终使用的地址)。
    """
    value = str(source or "").strip()
    if not value:
        return False, "图片地址为空", ""
    if not enabled:
        return True, "", value

    now = time.time()
    cached = _cache.get(value)
    if cached and cached[0] > now:
        return cached[1], cached[2], cached[3]

    if _is_local_source(value):
        ok, reason = _check_local(value, max_bytes)
        resolved = value
    else:
        ok, reason = await _check_url_target(value)
        resolved = value
        if ok:
            ok, reason, resolved = await _probe(value, max_bytes)

    if len(_cache) > 512:
        _cache.clear()
    _cache[value] = (now + _CACHE_TTL, ok, reason, resolved)
    return ok, reason, resolved
