import json
import time
import asyncio
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

def _parse_id_list(value) -> set:
    if isinstance(value, list):
        return set(str(g) for g in value if str(g).strip())
    if not isinstance(value, str):
        return set()
    return set(x.strip() for x in value.split(",") if x.strip())

def _serialize_id_list(s: set) -> str:
    return ",".join(sorted(s))

def _parse_group_templates(value) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        return json.loads(value)
    except Exception as e:
        # 【修复点 4】增加错误日志，防止配置因语法错误被静默清空
        logger.error(f"[group_welcome] 解析群欢迎模板 JSON 失败: {e}")
        return {}

def _serialize_group_templates(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False)

@register(
    "group_welcome",
    "lingyun",
    "入群欢迎插件：支持 OneBot 协议下的 @新成员、AI 个性化欢迎、群人数统计及黑白名单。",
    "1.0.7", # 建议升级版本号
)
class GroupWelcomePlugin(Star):
    # 【修复点 5】内存泄漏防护：增加过期记录清理机制
    _global_cooldown = {}
    _last_cleanup_time = 0

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._enable_member_count: bool = config.get("enable_member_count", True)
        self._enable_private_rules: bool = config.get("enable_private_rules", False)
        self._enable_ai_welcome: bool = config.get("enable_ai_welcome", False)
        self._whitelist: set = _parse_id_list(config.get("group_whitelist", ""))
        self._blacklist: set = _parse_id_list(config.get("group_blacklist", ""))

        # 【修复点 3】不再使用硬编码 sleep(3)，改用稳健的异步初始化任务
        asyncio.create_task(self._safe_register_handler())

    async def _safe_register_handler(self):
        """稳健的监听注册逻辑，带有重试机制"""
        retries = 0
        while retries < 15: # 最多等待约 75 秒
            client = self._get_client()
            if client:
                try:
                    @client.on_notice("group_increase")
                    async def _(event):
                        await self._on_notice(event)
                    logger.info("[group_welcome] OneBot 11 入群事件监听已成功注册。")
                    return
                except Exception as e:
                    logger.error(f"[group_welcome] 注册事件回调失败: {e}")
            
            retries += 1
            await asyncio.sleep(5)
        
        logger.warning("[group_welcome] 未能找到 OneBot 适配器，插件功能可能无法正常运行。")

    def _get_client(self):
        """【修复点 2】更健壮的客户端获取方式"""
        for adapter in self.context.platform_manager.get_insts():
            # 这里的逻辑虽然仍绑定 OneBot，但增加了对 bot 对象的有效性检查
            if "iocqhttp" in adapter.__class__.__name__.lower() or "onebot" in adapter.__class__.__name__.lower():
                if hasattr(adapter, "bot") and adapter.bot:
                    return adapter.bot
        return None

    def _clean_expired_cooldowns(self):
        """【修复点 5】清理过期的冷却记录，防止内存泄漏"""
        now = time.time()
        # 每小时清理一次
        if now - GroupWelcomePlugin._last_cleanup_time < 3600:
            return
        
        expired_keys = [
            k for k, timestamp in GroupWelcomePlugin._global_cooldown.items()
            if now - timestamp > 86400 # 清理超过 24 小时的记录
        ]
        for k in expired_keys:
            del GroupWelcomePlugin._global_cooldown[k]
        
        GroupWelcomePlugin._last_cleanup_time = now
        if expired_keys:
            logger.debug(f"[group_welcome] 已清理 {len(expired_keys)} 条过期冷却记录。")

    async def _on_notice(self, event):
        try:
            notice_type = event.get("notice_type")
            group_id = str(event.get("group_id", ""))
            user_id = str(event.get("user_id", ""))
        except Exception:
            return

        if notice_type != "group_increase" or not group_id or not user_id:
            return

        if not self._check_group_allowed(group_id):
            return

        # 冷却检查与清理
        self._clean_expired_cooldowns()
        cooldown_secs = self.config.get("cooldown_seconds", 300)
        key = f"{group_id}:{user_id}"
        now = time.time()
        
        if now - GroupWelcomePlugin._global_cooldown.get(key, 0) < cooldown_secs:
            return
        GroupWelcomePlugin._global_cooldown[key] = now

        client = self._get_client()
        if not client: return

        name = await self._get_member_name(client, group_id, user_id)
        count_text = ""
        if self._enable_member_count:
            count = await self._get_group_member_count(client, group_id)
            if count: count_text = f"\n你是当前群里第 {count} 位成员！"

        template = self._get_welcome_template(group_id)
        welcome_text = template.format(name=name, count_text=count_text)

        if self._enable_ai_welcome:
            ai_text = await self._gen_ai_welcome(name)
            if ai_text: welcome_text += f"\n\n✨ {ai_text}"

        await self._send_group_welcome(client, group_id, user_id, welcome_text)
        if self._enable_private_rules:
            await self._send_private_rules(client, user_id)

    def _check_group_allowed(self, group_id: str) -> bool:
        if self._whitelist:
            return group_id in self._whitelist
        return group_id not in self._blacklist

    def _get_welcome_template(self, group_id: str) -> str:
        templates = self._load_group_templates()
        return templates.get(group_id, self.config.get("welcome_template", "🎉 欢迎 {name} 加入本群！很高兴认识你～{count_text}"))

    async def _get_member_name(self, client, group_id: str, user_id: str) -> str:
        try:
            res = await client.api.call_action("get_group_member_info", group_id=int(group_id), user_id=int(user_id), no_cache=True)
            return res.get("card") or res.get("nickname") or user_id
        except Exception: return user_id

    async def _get_group_member_count(self, client, group_id: str):
        try:
            res = await client.api.call_action("get_group_info", group_id=int(group_id), no_cache=True)
            return res.get("member_count")
        except Exception: return None

    async def _send_group_welcome(self, client, group_id: str, user_id: str, text: str):
        try:
            message = [{"type": "at", "data": {"qq": user_id}}, {"type": "text", "data": {"text": f" {text}"}}]
            await client.api.call_action("send_group_msg", group_id=int(group_id), message=message)
        except Exception as e: logger.error(f"[group_welcome] 发送欢迎语失败: {e}")

    async def _send_private_rules(self, client, user_id: str):
        await asyncio.sleep(2)
        rules = self.config.get("group_rules", "📋 请遵守群规，友善交流！")
        try:
            await client.api.call_action("send_private_msg", user_id=int(user_id), message=rules)
        except Exception: pass

    async def _gen_ai_welcome(self, name: str) -> str:
        try:
            provider = self.context.get_using_provider()
            if not provider: return ""
            prompt_tmpl = self.config.get("ai_welcome_prompt", "请根据以下昵称，生成一句简短、温暖、有趣的入群欢迎语：{name}")
            resp = await provider.text_chat(prompt=prompt_tmpl.format(name=name), session_id=f"gw_{name}")
            return resp.completion_text.strip()
        except Exception: return ""

    def _save_switches(self):
        self.config["enable_member_count"] = self._enable_member_count
        self.config["enable_private_rules"] = self._enable_private_rules
        self.config["enable_ai_welcome"] = self._enable_ai_welcome
        self.config.save_config()

    def _save_lists(self):
        self.config["group_whitelist"] = _serialize_id_list(self._whitelist)
        self.config["group_blacklist"] = _serialize_id_list(self._blacklist)
        self.config.save_config()

    def _load_group_templates(self) -> dict:
        return _parse_group_templates(self.config.get("group_templates", "{}"))

    def _save_group_template(self, group_id: str, template: str):
        templates = self._load_group_templates()
        templates[group_id] = template
        self.config["group_templates"] = _serialize_group_templates(templates)
        self.config.save_config()

    def _del_group_template(self, group_id: str):
        templates = self._load_group_templates()
        if templates.pop(group_id, None):
            self.config["group_templates"] = _serialize_group_templates(templates)
            self.config.save_config()

    # ──────────────────────────────────────────
    # 指令管理
    # ──────────────────────────────────────────

    # 【修复点 1】补全 event 参数以符合框架规范
    @filter.command_group("welcome")
    def welcome(self, event: AstrMessageEvent):
        pass

    @welcome.command("count")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle_count(self, event: AstrMessageEvent, action: str = ""):
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
            yield event.plain_result(f"当前群人数统计：{status}\n用法：/welcome count on|off")

    @welcome.command("rules")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle_rules(self, event: AstrMessageEvent, action: str = ""):
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
            yield event.plain_result(f"当前私聊群规：{status}\n用法：/welcome rules on|off")

    @welcome.command("ai")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle_ai(self, event: AstrMessageEvent, action: str = ""):
        action = action.strip().lower()
        if action == "on":
            self._enable_ai_welcome = True
            self._save_switches()
            yield event.plain_result("✅ AI 个性化欢迎语已开启")
        elif action == "off":
            self._enable_ai_welcome = False
            self._save_switches()
            yield event.plain_result("🔕 AI 个性化欢迎语已关闭")
        else:
            status = "开启" if self._enable_ai_welcome else "关闭"
            yield event.plain_result(f"当前 AI 欢迎语：{status}\n用法：/welcome ai on|off")

    @welcome.command("set")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_group_template(self, event: AstrMessageEvent, action: str = ""):
        raw_msg = event.message_obj.message_str.strip()
        parts = raw_msg.split(maxsplit=2)
        text_content = parts[2].strip() if len(parts) > 2 else ""
        text = text_content.replace("｛", "{").replace("｝", "}")
        current_group_id = str(event.message_obj.group_id) if event.message_obj.group_id else ""
        
        target_group_id, final_content, op_type = current_group_id, text, "set"

        if not current_group_id:
            sub_parts = text.split(maxsplit=1)
            first_word = sub_parts[0] if sub_parts else ""
            if first_word in ["reset", "show"]:
                op_type = first_word
                if len(sub_parts) < 2:
                    yield event.plain_result(f"❌ 私聊请指定群号。")
                    return
                target_group_id = sub_parts[1].strip()
            elif first_word.isdigit():
                target_group_id = first_word
                final_content = sub_parts[1].strip() if len(sub_parts) > 1 else ""
            else:
                yield event.plain_result("❌ 私聊模式请先写群号。")
                return
        else:
            if text in ["reset", "show"]: op_type = text

        if op_type == "reset":
            self._del_group_template(target_group_id)
            yield event.plain_result(f"✅ 群 {target_group_id} 已恢复默认。")
        elif op_type == "show":
            tmpl = self._get_welcome_template(target_group_id)
            yield event.plain_result(f"📋 群 {target_group_id} 当前欢迎语：\n{tmpl}")
        elif op_type == "set":
            if not final_content:
                yield event.plain_result("❌ 内容不能为空。")
                return
            self._save_group_template(target_group_id, final_content)
            yield event.plain_result(f"✅ 群 {target_group_id} 欢迎语已设置：\n{final_content}")

    @welcome.command("wl")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def manage_whitelist(self, event: AstrMessageEvent, action: str = "", group_id: str = ""):
        action, group_id = action.strip().lower(), group_id.strip()
        if action == "list":
            yield event.plain_result("📋 白名单：" + ("、".join(sorted(self._whitelist)) if self._whitelist else "空"))
        elif action == "add" and group_id:
            self._whitelist.add(group_id); self._save_lists()
            yield event.plain_result(f"✅ 已加入白名单 {group_id}")
        elif action == "del" and group_id:
            self._whitelist.discard(group_id); self._save_lists()
            yield event.plain_result(f"✅ 已移除白名单 {group_id}")

    @welcome.command("bl")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def manage_blacklist(self, event: AstrMessageEvent, action: str = "", group_id: str = ""):
        action, group_id = action.strip().lower(), group_id.strip()
        if action == "list":
            yield event.plain_result("🚫 黑名单：" + ("、".join(sorted(self._blacklist)) if self._blacklist else "空"))
        elif action == "add" and group_id:
            self._blacklist.add(group_id); self._save_lists()
            yield event.plain_result(f"✅ 已加入黑名单 {group_id}")
        elif action == "del" and group_id:
            self._blacklist.discard(group_id); self._save_lists()
            yield event.plain_result(f"✅ 已移除黑名单 {group_id}")

    @welcome.command("status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def show_status(self, event: AstrMessageEvent, target_group: str = ""):
        # 【修复点 2】使用文本块 """ 优化长文本可读性
        target_group = target_group.strip()
        current_group_id = str(event.message_obj.group_id) if event.message_obj.group_id else ""
        query_group_id = target_group if target_group else current_group_id

        templates = self._load_group_templates()
        wl = "、".join(sorted(self._whitelist)) if self._whitelist else "（空）"
        bl = "、".join(sorted(self._blacklist)) if self._blacklist else "（空）"
        
        if query_group_id:
            source = "群专属" if query_group_id in templates else "全局默认"
            template_tip = f"📌 群 {query_group_id} 欢迎语 [{source}]：\n{self._get_welcome_template(query_group_id)}"
        else:
            template_tip = f"📌 已自定义群数：{len(templates)}\n💡 提示：私聊可带群号查询。"

        result = f"""📊 group_welcome 插件状态
{"─" * 24}
名单模式：{"白名单模式" if self._whitelist else "黑名单模式"}
白名单：{wl}
黑名单：{bl}
{"─" * 24}
群人数统计：{"✅ 开启" if self._enable_member_count else "🔕 关闭"}
私聊群规：{"✅ 开启" if self._enable_private_rules else "🔕 关闭"}
AI 个性欢迎：{"✅ 开启" if self._enable_ai_welcome else "🔕 关闭"}
冷却时间：{self.config.get("cooldown_seconds", 300)}s
{"─" * 24}
{template_tip}"""
        yield event.plain_result(result)

    async def terminate(self):
        logger.info("[group_welcome] 插件已卸载")
