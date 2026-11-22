"""QQ 官方机器人（qq_official）适配层。

AstrBot 的 QQ 官方适配器只把「消息」转换成 AstrBot 事件，官方 API 的入群事件
``GROUP_MEMBER_ADD`` 既没有对应的 qq-botpy intent 位，也没有事件解析器，插件因此
收不到入群通知。本模块用最小侵入的方式补齐这一层：

1. 给 qq-botpy 的 ``Intents`` 补上 ``GROUP_MEMBER_EVENT`` 位（仅群/C2C 场景）。
2. 给 ``ConnectionState`` 补一个 ``parse_group_member_add`` 解析器。
3. 把 ``on_group_member_add`` 回调挂到适配器的 botpy 客户端上。
4. 复用 AstrBot 适配器已有的会话发送能力发送欢迎语（Markdown @ + 图片），
   并用官方 HTTP 接口补上昵称/群人数查询。

注意：补丁必须在平台适配器建立连接之前安装。插件初始化早于平台初始化，
因此在插件 ``__init__`` 中调用即可；若通过 WebUI 热重载插件，需要重启 AstrBot
才能重新协商 intent。
"""

from __future__ import annotations

import re
import time
from typing import Any

from astrbot.api import logger

try:
    from astrbot.api.event import MessageChain
    from astrbot.api.platform import MessageSession, MessageType
except ImportError:  # pragma: no cover - 兼容旧版本 AstrBot 的导入路径
    from astrbot.core.message.message_event_result import MessageChain
    from astrbot.core.platform.message_session import MessageSession
    from astrbot.core.platform.message_type import MessageType

GROUP_MEMBER_EVENT_INTENT = 1 << 24
"""QQ 官方 API 的群成员事件 intent 位（GROUP_MEMBER_EVENT）。"""

QQOFFICIAL_PLATFORM_NAMES = ("qq_official", "qq_official_webhook")
"""AstrBot 中两个 QQ 官方机器人适配器的平台名。"""

DEFAULT_FALLBACK_NAME = "新朋友"
"""官方接口取不到昵称时使用的占位称呼。"""

_INTENTS_PATCH_FLAG = "_group_welcome_member_intent_patched"

MIN_ASTRBOT_VERSION = (4, 29, 0)
"""官机适配依赖的 AstrBot 最低版本。

低于该版本时：群主动消息路径缺失（适配器要求 msg_id，会静默跳过）、主动消息
不走 Markdown、或 At 组件不会转换成 ``<qqbot-at-user>``。
"""

# 昵称成功/失败分别缓存，避免每次入群都打接口（成员信息接口还在内邀阶段）。
_member_name_cache: dict[tuple[str, str], tuple[float, str]] = {}
_MEMBER_NAME_TTL_HIT = 3600.0
_MEMBER_NAME_TTL_MISS = 600.0
_MEMBER_NAME_CACHE_MAX = 2048

# 成员信息接口属于内邀能力；判定无权限后短时间内不再请求，避免刷日志。
_name_api_blocked_until = 0.0
_NAME_API_BLOCK_SECONDS = 600.0
_name_api_permission_warned = False

# 群人数缓存，减少 /v2/groups/{openid}/info 的调用次数。
_member_count_cache: dict[str, tuple[float, int]] = {}
_MEMBER_COUNT_TTL = 120.0


def is_qqofficial_adapter(adapter: Any) -> bool:
    """判断适配器是否为 QQ 官方机器人适配器。

    Args:
        adapter: AstrBot 平台适配器实例。

    Returns:
        是 QQ 官方适配器返回 True，否则返回 False。
    """
    try:
        meta = adapter.meta()
    except Exception:
        return False
    return getattr(meta, "name", "") in QQOFFICIAL_PLATFORM_NAMES


def _version_tuple(value: str) -> tuple[int, ...]:
    """把版本字符串转换成可比较的数字元组。

    Args:
        value: 形如 ``4.29.0`` / ``4.30.0-beta.1`` 的版本号。

    Returns:
        最多三段的数字元组；无法解析时返回空元组。
    """
    return tuple(int(part) for part in re.findall(r"\d+", value or "")[:3])


def check_astrbot_version() -> str:
    """检查 AstrBot 版本是否满足官机适配要求。

    官机链路依赖 AstrBot 4.29.0 起才完整的能力：群主动消息、主动消息走
    Markdown、``At`` 组件转换为 ``<qqbot-at-user>``。低于该版本时插件不会崩溃，
    但欢迎语可能发不出去或丢失 @ / 图片，因此启动时给出显式告警。

    Returns:
        版本过低时返回告警文案；满足要求或取不到版本时返回空字符串。
    """
    try:
        import astrbot

        version = str(getattr(astrbot, "__version__", "") or "")
    except Exception:
        return ""

    current = _version_tuple(version)
    if not current or current >= MIN_ASTRBOT_VERSION:
        return ""

    required = ".".join(str(part) for part in MIN_ASTRBOT_VERSION)
    return (
        f"当前 AstrBot {version} 低于官机适配所需的最低版本 v{required}："
        "群主动消息可能被适配器静默跳过，或 @ / 图片退化为纯文本，建议升级。"
    )


def patch_botpy_for_member_event() -> bool:
    """为 qq-botpy 补充 GROUP_MEMBER_ADD 所需的 intent 位与事件解析器。

    该函数是幂等的，重复调用不会叠加包装。必须在平台连接建立之前调用。

    Returns:
        补丁安装成功返回 True；qq-botpy 缺失或结构不兼容时返回 False。
    """
    try:
        from botpy.connection import ConnectionState
        from botpy.flags import Intents
    except Exception as exc:
        logger.warning(f"[group_welcome] 未找到 qq-botpy，跳过官机适配: {exc}")
        return False

    if not getattr(Intents, _INTENTS_PATCH_FLAG, False):
        original_init = Intents.__init__

        def patched_init(self, **kwargs: bool) -> None:
            """构造 Intents 后追加群成员事件 intent。"""
            original_init(self, **kwargs)
            # 只在群/C2C 场景追加，避免纯频道机器人被服务端拒绝。
            if getattr(self, "public_messages", False):
                self.value |= GROUP_MEMBER_EVENT_INTENT

        Intents.__init__ = patched_init  # type: ignore[method-assign]
        setattr(Intents, _INTENTS_PATCH_FLAG, True)

    if not hasattr(ConnectionState, "parse_group_member_add"):

        def parse_group_member_add(self, payload: Any) -> None:
            """解析 GROUP_MEMBER_ADD 并派发给客户端的同名回调。"""
            data = payload.get("d") if isinstance(payload, dict) else None
            self._dispatch("group_member_add", data if isinstance(data, dict) else {})

        ConnectionState.parse_group_member_add = parse_group_member_add  # type: ignore[attr-defined]

    return True


def attach_member_handler(adapter: Any, handler: Any) -> bool:
    """把入群事件回调挂到 QQ 官方适配器的 botpy 客户端上。

    qq-botpy 通过 ``getattr(client, "on_<event>")`` 查找回调，实例属性即可生效。

    Args:
        adapter: QQ 官方适配器实例。
        handler: ``async def handler(payload: dict)`` 形式的回调。

    Returns:
        挂载成功返回 True。
    """
    client = getattr(adapter, "client", None)
    if client is None:
        return False
    client.on_group_member_add = handler
    return True


def detach_member_handler(adapter: Any) -> None:
    """移除已注册的入群事件回调，避免插件卸载后回调悬空。

    Args:
        adapter: QQ 官方适配器实例。
    """
    client = getattr(adapter, "client", None)
    if client is None:
        return
    try:
        del client.on_group_member_add
    except AttributeError:
        pass


def _describe_send_error(exc: Exception) -> str:
    """把官机发送失败的错误码翻译成可操作的提示。

    Args:
        exc: 发送消息时抛出的异常。

    Returns:
        面向用户的错误说明。
    """
    text = str(exc)
    if "40034105" in text:
        return (
            "QQ 官方机器人没有主动消息权限（40034105），"
            "请到 QQ 开放平台确认机器人是否已开通主动消息"
        )
    if "40054010" in text:
        return "QQ 官方机器人不允许发送 URL（40054010），请从欢迎语中移除链接"
    if "40034100" in text:
        return "QQ 官方机器人群主动消息超出频控（40034100），请降低触发频率"
    return f"QQ 官方欢迎语发送失败: {exc}"


def _is_remote_image(image_url: str) -> bool:
    """判断图片地址是否为公网 HTTP(S) 地址。

    Args:
        image_url: 图片配置项。

    Returns:
        公网地址返回 True；空值或本地路径返回 False。
    """
    return bool(image_url) and image_url.lower().startswith(("http://", "https://"))


def _build_image_chain(image_url: str) -> MessageChain:
    """把图片配置项转换为可发送的消息链。

    Args:
        image_url: HTTP(S) 图片地址或本地路径（含 ``file:///`` 形式）。

    Returns:
        只包含一张图片的 MessageChain。

    Raises:
        ValueError: 图片地址为空或格式不受支持。
    """
    if not image_url:
        raise ValueError("empty image url")
    if _is_remote_image(image_url):
        return MessageChain().url_image(image_url)
    path = image_url[8:] if image_url.lower().startswith("file:///") else image_url
    return MessageChain().file_image(path)


async def send_group_welcome(
    context: Any,
    adapter: Any,
    group_id: str,
    user_id: str,
    text: str,
    image_url: str = "",
) -> None:
    """通过 QQ 官方机器人发送入群欢迎语（含 @ 新成员与可选图片）。

    官机入群事件没有可用的 msg_id，因此这里发送的是主动消息，会消耗群主动消息
    配额（每个群每天 1000 条）。为了只占一条配额，公网图片会以 Markdown 内嵌
    图片的语法塞进同一条消息；本地图片无法内嵌，才拆成第二条富媒体消息。

    Args:
        context: AstrBot 插件 Context。
        adapter: QQ 官方适配器实例。
        group_id: 群 OpenID。
        user_id: 新成员的 OpenID。
        text: 欢迎语正文。
        image_url: 可选的欢迎图片地址或本地路径。
    """
    # Markdown 消息支持 ![说明](url)，平台会自动下载转存该图片。
    inline_image = _is_remote_image(image_url)
    markdown_text = f"{text}\n\n![欢迎图片]({image_url})" if inline_image else text

    try:
        session = MessageSession(
            adapter.meta().id,
            MessageType.GROUP_MESSAGE,
            group_id,
        )
        # 官机适配器只有在会话场景已知时才允许群主动发送。
        remember_scene = getattr(adapter, "remember_session_scene", None)
        if callable(remember_scene):
            remember_scene(group_id, "group")

        chain = MessageChain().at("", user_id).message(f" {markdown_text}")
        chain.use_markdown_ = True
        await context.send_message(session, chain)
    except Exception as exc:
        logger.error(f"[group_welcome] {_describe_send_error(exc)}")
        return

    if not image_url or inline_image:
        return
    try:
        await context.send_message(session, _build_image_chain(image_url))
    except Exception as exc:
        logger.warning(f"[group_welcome] 欢迎图片发送失败: {exc}")


async def send_private_rules(
    context: Any,
    adapter: Any,
    user_id: str,
    rules: str,
) -> None:
    """通过 QQ 官方机器人私聊发送群规。

    官方单聊同样使用主动消息，用户可以在客户端关闭「允许主动发送」，
    因此失败只记录警告，不影响群内欢迎。

    Args:
        context: AstrBot 插件 Context。
        adapter: QQ 官方适配器实例。
        user_id: 用户 OpenID。
        rules: 群规文本。
    """
    try:
        session = MessageSession(
            adapter.meta().id,
            MessageType.FRIEND_MESSAGE,
            user_id,
        )
        await context.send_message(session, MessageChain().message(rules))
    except Exception as exc:
        logger.warning(f"[group_welcome] 官机私聊群规发送失败: {exc}")


def _is_permission_error(message: str) -> bool:
    """判断接口错误是否为「应用无接口访问权限」这类权限问题。

    Args:
        message: 接口异常文本。

    Returns:
        属于权限类错误返回 True。
    """
    lowered = message.lower()
    if "无接口访问权限" in message or "无权限" in message:
        return True
    return any(keyword in lowered for keyword in ("permission", "forbidden", "403"))


async def get_member_name(
    adapter: Any,
    group_id: str,
    user_id: str,
    fallback: str = DEFAULT_FALLBACK_NAME,
) -> str:
    """查询群成员昵称，失败时回退到占位称呼。

    官方群成员信息接口（``/v2/groups/{group_id}/members/{user_id}``）返回
    ``username``，但该能力仍在灰度开放。未开通时接口返回「应用无接口访问
    权限」，此时会暂停请求一段时间并直接使用 ``fallback``。

    Args:
        adapter: QQ 官方适配器实例。
        group_id: 群 OpenID。
        user_id: 成员 OpenID。
        fallback: 取不到昵称时使用的称呼。

    Returns:
        成员昵称或占位称呼。
    """
    global _name_api_blocked_until, _name_api_permission_warned

    key = (group_id, user_id)
    now = time.time()
    cached = _member_name_cache.get(key)
    if cached and cached[0] > now:
        return cached[1]
    if now < _name_api_blocked_until:
        return fallback

    name = ""
    try:
        from botpy.http import Route

        route = Route(
            "GET",
            "/v2/groups/{group_openid}/members/{member_openid}",
            group_openid=group_id,
            member_openid=user_id,
        )
        response = await adapter.client.api._http.request(route)
        if isinstance(response, dict):
            name = str(response.get("username") or "").strip()
    except Exception as exc:
        if _is_permission_error(str(exc)):
            _name_api_blocked_until = now + _NAME_API_BLOCK_SECONDS
            if not _name_api_permission_warned:
                _name_api_permission_warned = True
                logger.warning(
                    "[group_welcome] QQ 官方群成员信息接口未开通权限（内邀能力），"
                    "欢迎语暂用占位昵称；申请到权限后会自动生效。"
                )
        else:
            logger.debug(f"[group_welcome] 获取官机成员昵称失败: {exc}")

    if name:
        _member_name_cache[key] = (now + _MEMBER_NAME_TTL_HIT, name)
        resolved = name
    else:
        _member_name_cache[key] = (now + _MEMBER_NAME_TTL_MISS, fallback)
        resolved = fallback

    if len(_member_name_cache) > _MEMBER_NAME_CACHE_MAX:
        _member_name_cache.pop(next(iter(_member_name_cache)), None)
    return resolved


async def get_group_member_count(adapter: Any, group_id: str) -> int | None:
    """查询群人数。

    Args:
        adapter: QQ 官方适配器实例。
        group_id: 群 OpenID。

    Returns:
        群人数；接口不可用或字段缺失时返回 None。
    """
    now = time.time()
    cached = _member_count_cache.get(group_id)
    if cached and cached[0] > now:
        return cached[1]

    try:
        from botpy.http import Route

        route = Route(
            "GET",
            "/v2/groups/{group_openid}/info",
            group_openid=group_id,
        )
        response = await adapter.client.api._http.request(route)
        if isinstance(response, dict):
            for field in ("group_member_num", "member_count"):
                value = response.get(field)
                if value is not None:
                    count = int(value)
                    _member_count_cache[group_id] = (now + _MEMBER_COUNT_TTL, count)
                    return count
    except Exception as exc:
        logger.debug(f"[group_welcome] 获取官机群人数失败: {exc}")
    return None
