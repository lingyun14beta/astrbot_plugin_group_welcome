import json
import time
import asyncio
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

# ================================================================
# 黑白名单逻辑：
#   白名单不为空 → 只处理白名单内的群，黑名单完全失效
#   白名单为空   → 黑名单生效，黑名单内的群跳过，其余正常欢迎
# ================================================================


def _parse_id_list(value) -> set:
    """将逗号分隔的群号字符串解析为 set，兼容旧版 list 类型。"""
    if isinstance(value, list):
        return set(str(g) for g in value if str(g).strip())
    if not isinstance(value, str):
        return set()
    return set(x.strip() for x in value.split(",") if x.strip())


def _serialize_id_list(s: set) -> str:
    return ",".join(sorted(s))


def _parse_group_templates(value) -> dict:
    """将 JSON 字符串解析为 dict，兼容旧版 dict 类型。"""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {}


def _serialize_group_templates(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False)


@register(
    "group_welcome",
    "YourName",
    "入群欢迎插件：@新成员、按群配置欢迎语、AI个性化欢迎、实时群人数、私聊群规、防刷冷却、群黑白名单",
    "1.0.6",
)
class GroupWelcomePlugin(Star):
    # 【防双发补丁】将冷却字典提升为类属性，防止热重载导致多重监听
    _global_cooldown = {}

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self._enable_member_count: bool = config.get("enable_member_count", True)
        self._enable_private_rules: bool = config.get("enable_private_rules", True)
        self._enable_ai_welcome: bool = config.get("enable_ai_welcome", False)

        # 从逗号字符串解析黑白名单
        self._whitelist: set = _parse_id_list(config.get("group_whitelist", ""))
        self._blacklist: set = _parse_id_list(config.get("group_blacklist", ""))

        asyncio.create_task(self._register_notice_handler())

    # ──────────────────────────────────────────
    # 持久化
    # ──────────────────────────────────────────

    def _save_switches(self):
        self.config["enable_member_count"] = self._enable_member_count
        self.config["enable_private_rules"] = self._enable_private_rules
        self.config["enable_ai_welcome"] = self._enable_ai_welcome
        self.config.save_config()

    def _save_lists(self):
        """保存为逗号分隔字符串（对应 schema 的 string 类型）"""
        self.config["group_whitelist"] = _serialize_id_list(self._whitelist)
        self.config["group_blacklist"] = _serialize_id_list(self._blacklist)
        self.config.save_config()

    def _load_group_templates(self) -> dict:
        return _parse_group_templates(self.config.get("group_templates", "{}"))

    def _save_group_templates(self, templates: dict):
        self.config["group_templates"] = _serialize_group_templates(templates)
        self.config.save_config()

    def _save_group_template(self, group_id: str, template: str):
        templates = self._load_group_templates()
        templates[group_id] = template
        self._save_group_templates(templates)

    def _del_group_template(self, group_id: str):
        templates = self._load_group_templates()
        templates.pop(group_id, None)
        self._save_group_templates(templates)

    def _get_welcome_template(self, group_id: str) -> str:
        """获取欢迎语：优先群专属，否则全局默认"""
        templates = self._load_group_templates()
        if group_id in templates:
            return templates[group_id]
        return self.config.get(
            "welcome_template",
            "🎉 欢迎 {name} 加入本群！很高兴认识你～{count_text}",
        )

    # ──────────────────────────────────────────
    # 黑白名单检查
    # ──────────────────────────────────────────

    def _check_group_allowed(self, group_id: str) -> bool:
        if self._whitelist:
            return group_id in self._whitelist
        if group_id in self._blacklist:
            return False
        return True

    # ──────────────────────────────────────────
    # 获取 aiocqhttp client
    # ──────────────────────────────────────────

    def _get_client(self):
        for adapter in self.context.platform_manager.get_insts():
            if "iocqhttp" in adapter.__class__.__name__.lower():
                if hasattr(adapter, "bot"):
                    return adapter.bot
        return None

    # ──────────────────────────────────────────
    # 入群事件监听
    # ──────────────────────────────────────────

    async def _register_notice_handler(self):
        await asyncio.sleep(3)
        try:
            client = self._get_client()
            if client:

                @client.on_notice("group_increase")
                async def _(event):
                    await self._on_notice(event)

                logger.info(
                    "[group_welcome] 已注册入群事件监听（on_notice group_increase）"
                )
            else:
                logger.warning(
                    "[group_welcome] 未找到 aiocqhttp 适配器，入群欢迎不可用"
                )
        except Exception as e:
            logger.warning(f"[group_welcome] 注册事件监听失败: {e}")

    async def _on_notice(self, event):
        try:
            notice_type = event["notice_type"]
            group_id = str(event["group_id"])
            user_id = str(event["user_id"])
        except (KeyError, TypeError):
            return

        if notice_type != "group_increase":
            return

        if not self._check_group_allowed(group_id):
            logger.info(f"[group_welcome] 群 {group_id} 不在允许范围，跳过")
            return

        cooldown_secs = self.config.get("cooldown_seconds", 300)
        key = f"{group_id}:{user_id}"
        now = time.time()

        # 使用类属性检查冷却，防止热重载产生幽灵监听器
        if now - GroupWelcomePlugin._global_cooldown.get(key, 0) < cooldown_secs:
            logger.info(f"[group_welcome] {user_id} 冷却中，跳过欢迎")
            return
        GroupWelcomePlugin._global_cooldown[key] = now

        client = self._get_client()
        if not client:
            return

        name = await self._get_member_name(client, group_id, user_id)

        count_text = ""
        if self._enable_member_count:
            count = await self._get_group_member_count(client, group_id)
            if count is not None:
                count_text = f"\n你是当前群里第 {count} 位成员！"

        template = self._get_welcome_template(group_id)
        welcome_text = template.format(name=name, count_text=count_text)

        if self._enable_ai_welcome:
            ai_text = await self._gen_ai_welcome(name)
            if ai_text:
                welcome_text += f"\n\n✨ {ai_text}"

        await self._send_group_welcome(client, group_id, user_id, welcome_text)

        if self._enable_private_rules:
            await self._send_private_rules(client, user_id)

    # ──────────────────────────────────────────
    # API 封装
    # ──────────────────────────────────────────

    async def _get_member_name(self, client, group_id: str, user_id: str) -> str:
        try:
            result = await client.api.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=True,
            )
            return result.get("card") or result.get("nickname") or user_id
        except Exception as e:
            logger.warning(f"[group_welcome] 获取成员昵称失败: {e}")
            return user_id

    async def _get_group_member_count(self, client, group_id: str):
        try:
            result = await client.api.call_action(
                "get_group_info",
                group_id=int(group_id),
                no_cache=True,
            )
            return result.get("member_count")
        except Exception as e:
            logger.warning(f"[group_welcome] 获取群人数失败: {e}")
            return None

    async def _send_group_welcome(self, client, group_id: str, user_id: str, text: str):
        try:
            message = [
                {"type": "at", "data": {"qq": user_id}},
                {"type": "text", "data": {"text": f" {text}"}},
            ]
            await client.api.call_action(
                "send_group_msg",
                group_id=int(group_id),
                message=message,
            )
        except Exception as e:
            logger.error(f"[group_welcome] 发送群欢迎失败: {e}")

    async def _send_private_rules(self, client, user_id: str):
        await asyncio.sleep(2)
        rules: str = self.config.get("group_rules", "📋 请遵守群规，友善交流！")
        try:
            await client.api.call_action(
                "send_private_msg",
                user_id=int(user_id),
                message=rules,
            )
        except Exception as e:
            logger.warning(f"[group_welcome] 私聊群规失败（对方可能未加好友）: {e}")

    async def _gen_ai_welcome(self, name: str) -> str:
        try:
            # 直接获取 AstrBot 全局当前正在使用的提供商
            provider = self.context.get_using_provider()

            if not provider:
                logger.warning(
                    "[group_welcome] 没有可用的 LLM Provider，跳过 AI 欢迎语"
                )
                return ""

            ai_prompt: str = self.config.get(
                "ai_welcome_prompt",
                "请根据以下昵称，生成一句简短、温暖、有趣的入群欢迎语（不超过30字，不要带引号）：{name}",
            )

            resp = await provider.text_chat(
                prompt=ai_prompt.format(name=name),
                session_id=f"group_welcome_ai_{name}",
            )
            return resp.completion_text.strip()

        except Exception as e:
            logger.warning(f"[group_welcome] AI 欢迎语生成失败: {e}")
            return ""

    # ──────────────────────────────────────────
    # 管理指令组
    # ──────────────────────────────────────────

    @filter.command_group("welcome")
    def welcome(self):
        pass

    @welcome.command("count")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle_count(self, event: AstrMessageEvent, action: str = ""):
        """/welcome count on|off  开关群人数统计"""
        action = action.strip().lower()
        if action == "on":
            self._enable_member_count = True
            self._save_switches()
            yield event.plain_result("✅ 群人数统计已开启")
        elif action == "off":
            self._enable_member_count = False
            self._save_switches()
            yield event.plain_result("🔕 群人数统计已关闭")
        else:
            status = "开启" if self._enable_member_count else "关闭"
            yield event.plain_result(
                f"当前群人数统计：{status}\n用法：/welcome count on|off"
            )

    @welcome.command("rules")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle_rules(self, event: AstrMessageEvent, action: str = ""):
        """/welcome rules on|off  开关私聊群规"""
        action = action.strip().lower()
        if action == "on":
            self._enable_private_rules = True
            self._save_switches()
            yield event.plain_result("✅ 私聊群规已开启")
        elif action == "off":
            self._enable_private_rules = False
            self._save_switches()
            yield event.plain_result("🔕 私聊群规已关闭")
        else:
            status = "开启" if self._enable_private_rules else "关闭"
            yield event.plain_result(
                f"当前私聊群规：{status}\n用法：/welcome rules on|off"
            )

    @welcome.command("ai")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle_ai(self, event: AstrMessageEvent, action: str = ""):
        """/welcome ai on|off  开关AI个性化欢迎语"""
        action = action.strip().lower()
        if action == "on":
            self._enable_ai_welcome = True
            self._save_switches()
            yield event.plain_result("✅ AI个性化欢迎语已开启")
        elif action == "off":
            self._enable_ai_welcome = False
            self._save_switches()
            yield event.plain_result("🔕 AI个性化欢迎语已关闭")
        else:
            status = "开启" if self._enable_ai_welcome else "关闭"
            yield event.plain_result(
                f"当前AI欢迎语：{status}\n用法：/welcome ai on|off"
            )

    @welcome.command("set")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_group_template(self, event: AstrMessageEvent, action: str = ""):
        """
        设置欢迎语
        用法：
        群聊中：/welcome set 欢迎 {name} 加入我们！
        私聊中：/welcome set <群号> 欢迎 {name} 加入我们！
        """
        # 1. 获取最原始的消息内容
        raw_msg = event.message_obj.message_str.strip()

        # 2. 手动切割字符串，完美保留欢迎语中的空格
        parts = raw_msg.split(maxsplit=2)

        text_content = ""
        if len(parts) > 2:
            text_content = parts[2].strip()

        # 3. 自动修正中文大括号
        text = text_content.replace("｛", "{").replace("｝", "}")

        # 获取当前群号
        current_group_id = (
            str(event.message_obj.group_id) if event.message_obj.group_id else ""
        )

        target_group_id = current_group_id
        final_content = text
        op_type = "set"

        if not current_group_id:
            # 私聊模式下的处理
            sub_parts = text.split(maxsplit=1)
            first_word = sub_parts[0] if sub_parts else ""

            if first_word in ["reset", "show"]:
                op_type = first_word
                if len(sub_parts) < 2:
                    yield event.plain_result(
                        f"❌ 私聊请指定群号，例如：/welcome set {first_word} 123456"
                    )
                    return
                target_group_id = sub_parts[1].strip()
            elif first_word.isdigit():
                target_group_id = first_word
                final_content = sub_parts[1].strip() if len(sub_parts) > 1 else ""
            else:
                yield event.plain_result(
                    "❌ 私聊模式请先写群号。例如：/welcome set 123456 欢迎语..."
                )
                return
        else:
            # 群聊模式下的处理
            if text in ["reset", "show"]:
                op_type = text
            else:
                final_content = text

        templates = self._load_group_templates()

        if op_type == "reset":
            self._del_group_template(target_group_id)
            yield event.plain_result(f"✅ 群 {target_group_id} 已恢复默认欢迎语。")

        elif op_type == "show":
            tmpl = self._get_welcome_template(target_group_id)
            src = "群专属" if target_group_id in templates else "全局默认"
            yield event.plain_result(
                f"📋 群 {target_group_id} 当前欢迎语 ({src})：\n{tmpl}"
            )

        elif op_type == "set":
            if not final_content:
                yield event.plain_result("❌ 欢迎语内容不能为空。")
                return
            self._save_group_template(target_group_id, final_content)
            yield event.plain_result(
                f"✅ 群 {target_group_id} 欢迎语已设置：\n{final_content}"
            )

    @welcome.command("wl")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def manage_whitelist(
        self, event: AstrMessageEvent, action: str = "", group_id: str = ""
    ):
        """/welcome wl add|del|list <群号>"""
        action = action.strip().lower()
        group_id = group_id.strip()

        if action == "list":
            if not self._whitelist:
                yield event.plain_result("白名单为空（当前使用黑名单模式）。")
            else:
                yield event.plain_result(
                    "📋 白名单群列表：\n" + "\n".join(sorted(self._whitelist))
                )
        elif action == "add" and group_id:
            self._whitelist.add(group_id)
            self._save_lists()
            yield event.plain_result(
                f"✅ 已将群 {group_id} 加入白名单\n"
                "⚠️ 白名单已启用，现在只有白名单内的群会触发欢迎"
            )
        elif action == "del" and group_id:
            self._whitelist.discard(group_id)
            self._save_lists()
            tip = (
                "\n白名单已清空，自动切换回黑名单模式。" if not self._whitelist else ""
            )
            yield event.plain_result(f"✅ 已将群 {group_id} 从白名单移除。{tip}")
        else:
            yield event.plain_result(
                "用法：\n/welcome wl add <群号>\n/welcome wl del <群号>\n/welcome wl list"
            )

    @welcome.command("bl")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def manage_blacklist(
        self, event: AstrMessageEvent, action: str = "", group_id: str = ""
    ):
        """/welcome bl add|del|list <群号>"""
        action = action.strip().lower()
        group_id = group_id.strip()

        if action == "list":
            if not self._blacklist:
                yield event.plain_result("黑名单为空。")
            else:
                yield event.plain_result(
                    "🚫 黑名单群列表：\n" + "\n".join(sorted(self._blacklist))
                )
        elif action == "add" and group_id:
            self._blacklist.add(group_id)
            self._save_lists()
            if self._whitelist:
                yield event.plain_result(
                    f"✅ 已将群 {group_id} 加入黑名单\n"
                    "⚠️ 当前白名单不为空，黑名单暂时不生效"
                )
            else:
                yield event.plain_result(f"✅ 已将群 {group_id} 加入黑名单")
        elif action == "del" and group_id:
            self._blacklist.discard(group_id)
            self._save_lists()
            yield event.plain_result(f"✅ 已将群 {group_id} 从黑名单移除")
        else:
            yield event.plain_result(
                "用法：\n/welcome bl add <群号>\n/welcome bl del <群号>\n/welcome bl list"
            )

    @welcome.command("status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def show_status(self, event: AstrMessageEvent, target_group: str = ""):
        """
        查看插件当前所有状态
        用法:
        /welcome status          (群聊: 查本群 / 私聊: 查全局)
        /welcome status <群号>   (私聊查指定群)
        """
        # 清理可能带入的空格
        target_group = target_group.strip()

        # 智能识别要查询的群号
        # 1. 优先使用用户输入的 target_group
        # 2. 如果没输入，检查是否在群聊中（提取当前群号）
        current_group_id = (
            str(event.message_obj.group_id) if event.message_obj.group_id else ""
        )
        query_group_id = target_group if target_group else current_group_id

        # 加载数据
        templates = self._load_group_templates()
        wl = (
            "、".join(sorted(self._whitelist))
            if self._whitelist
            else "（空，使用黑名单模式）"
        )
        bl = "、".join(sorted(self._blacklist)) if self._blacklist else "（空）"
        list_mode = "白名单模式（只欢迎白名单群）" if self._whitelist else "黑名单模式"

        # 根据是否有查询的群号，动态生成底部提示信息
        if query_group_id:
            source = "群专属" if query_group_id in templates else "全局默认"
            template_text = self._get_welcome_template(query_group_id)
            template_tip = (
                f"📌 目标群 ({query_group_id}) 欢迎语 [{source}]：\n{template_text}"
            )
        else:
            template_tip = f"📌 已自定义专属欢迎语的群数：{len(templates)}\n💡 提示：若要查看具体群欢迎语，请附带群号。例如：/welcome status 123456"

        # 输出最终面板
        yield event.plain_result(
            f"📊 group_welcome 插件状态\n"
            f"{'─' * 24}\n"
            f"名单模式：{list_mode}\n"
            f"白名单：{wl}\n"
            f"黑名单：{bl}\n"
            f"{'─' * 24}\n"
            f"群人数统计：{'✅ 开启' if self._enable_member_count else '🔕 关闭'}\n"
            f"私聊群规：{'✅ 开启' if self._enable_private_rules else '🔕 关闭'}\n"
            f"AI个性化欢迎：{'✅ 开启' if self._enable_ai_welcome else '🔕 关闭'}\n"
            f"AI 使用模型：全局默认\n"
            f"冷却时间：{self.config.get('cooldown_seconds', 300)} 秒\n"
            f"{'─' * 24}\n"
            f"{template_tip}"
        )

    async def terminate(self):
        logger.info("[group_welcome] 插件已卸载")
