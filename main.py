import json
import time
import os
import asyncio
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

# 使用插件所在目录的绝对路径，确保文件读写安全
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOLDOWN_FILE = os.path.join(BASE_DIR, "cooldowns.json")

def _parse_id_list(value) -> set:
    """解析逗号分隔的群号字符串为集合。"""
    if isinstance(value, list):
        return set(str(item) for item in value if str(item).strip())
    if not isinstance(value, str):
        return set()
    return set(item.strip() for item in value.split(",") if item.strip())


def _serialize_id_list(id_set: set) -> str:
    """序列化群号集合为字符串。"""
    return ",".join(sorted(id_set))


def _parse_group_templates(value) -> dict:
    """解析群欢迎语模板 JSON。"""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        return json.loads(value)
    except Exception as e:
        logger.error(f"[group_welcome] 解析群模板失败: {e}")
        return {}


def _serialize_group_templates(templates: dict) -> str:
    """序列化群欢迎语模板。"""
    return json.dumps(templates, ensure_ascii=False)


@register(
    "group_welcome",
    "YourName",
    "入群欢迎插件：支持 OneBot 协议下的 @新成员、AI 个性化欢迎、群人数统计及黑白名单。",
    "4.1.8",
)
class GroupWelcomePlugin(Star):
    # 类属性：全局冷却记录
    _global_cooldown = {}
    _last_cleanup_time = 0

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        
        # 锁初始化 (Python 3.10+ 安全)
        self._lock = asyncio.Lock()
        
        # 运行状态标记
        self._is_running = True
        
        # 配置加载
        self._enable_member_count: bool = config.get("enable_member_count", True)
        self._enable_private_rules: bool = config.get("enable_private_rules", False)
        self._enable_ai_welcome: bool = config.get("enable_ai_welcome", False)
        
        self._whitelist: set = _parse_id_list(config.get("group_whitelist", ""))
        self._blacklist: set = _parse_id_list(config.get("group_blacklist", ""))

        # 加载持久化的冷却数据
        self._load_cooldowns()

        # 启动事件监听注册任务
        asyncio.create_task(self._safe_register_handler())

    # ──────────────────────────────────────────
    # 生命周期管理
    # ──────────────────────────────────────────

    async def _safe_register_handler(self):
        """稳健的事件监听注册逻辑。"""
        max_retries = 15
        for _ in range(max_retries):
            if not self._is_running: return 
            
            client = self._get_client()
            if client:
                try:
                    # 获取 OneBot 适配器的原始 bot 对象进行事件监听
                    # 注意：这依赖于 client 暴露 on_notice 装饰器
                    if hasattr(client, "on_notice"):
                        @client.on_notice("group_increase")
                        async def _group_increase_handler(event):
                            if not self._is_running: return
                            await self._on_notice(event)
                        
                        logger.info("[group_welcome] OneBot 11 入群事件监听已成功注册。")
                        return
                    else:
                        # 尝试兼容其他适配器结构，或等待加载
                        pass
                except Exception as e:
                    logger.error(f"[group_welcome] 注册监听失败: {e}")
            
            await asyncio.sleep(5)
        
        logger.warning("[group_welcome] 超时未找到 OneBot 适配器，插件功能可能受限。")

    async def terminate(self):
        """插件卸载回调。"""
        self._is_running = False
        self._save_cooldowns()
        logger.info("[group_welcome] 插件已卸载，冷却数据已保存。")

    # ──────────────────────────────────────────
    # 冷却数据持久化
    # ──────────────────────────────────────────

    def _load_cooldowns(self):
        """从文件加载冷却数据。"""
        if not os.path.exists(COOLDOWN_FILE):
            return
        try:
            with open(COOLDOWN_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                now = time.time()
                count = 0
                for k, v in data.items():
                    if now - v < 86400:
                        GroupWelcomePlugin._global_cooldown[k] = v
                        count += 1
                logger.debug(f"[group_welcome] 已加载 {count} 条有效冷却记录。")
        except Exception as e:
            logger.warning(f"[group_welcome] 加载冷却文件失败: {e}")

    def _save_cooldowns(self):
        """保存冷却数据到文件。"""
        try:
            with open(COOLDOWN_FILE, 'w', encoding='utf-8') as f:
                json.dump(GroupWelcomePlugin._global_cooldown, f)
        except Exception as e:
            logger.warning(f"[group_welcome] 保存冷却数据失败: {e}")

    # ──────────────────────────────────────────
    # 核心逻辑
    # ──────────────────────────────────────────

    def _get_client(self):
        try:
            for adapter in self.context.platform_manager.get_insts():
                name = adapter.__class__.__name__.lower()
                if "iocqhttp" in name or "onebot" in name:
                    if hasattr(adapter, "bot") and adapter.bot:
                        return adapter.bot
        except Exception:
            pass
        return None

    def _clean_expired_cooldowns(self):
        now = time.time()
        if now - GroupWelcomePlugin._last_cleanup_time < 3600:
            return
        
        expired = [k for k, ts in GroupWelcomePlugin._global_cooldown.items() if now - ts > 86400]
        for key in expired:
            del GroupWelcomePlugin._global_cooldown[key]
        
        GroupWelcomePlugin._last_cleanup_time = now
        self._save_cooldowns()

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

        self._clean_expired_cooldowns()
        
        key = f"{group_id}:{user_id}"
        cooldown = self.config.get("cooldown_seconds", 300)
        
        async with self._lock:
            now = time.time()
            if now - GroupWelcomePlugin._global_cooldown.get(key, 0) < cooldown:
                return
            GroupWelcomePlugin._global_cooldown[key] = now

        client = self._get_client()
        if not client: return

        name = await self._get_member_name(client, group_id, user_id)
        
        count_text = ""
        if self._enable_member_count:
            count = await self._get_group_member_count(client, group_id)
            if count: count_text = f"\n你是当前群里第 {count} 位成员！"

        # 生成欢迎语
        template = self._get_welcome_template(group_id)
        
        try:
            welcome_text = template.format(name=name, count_text=count_text)
        except Exception as e:
            logger.warning(f"[group_welcome] 群 {group_id} 欢迎语模板格式错误: {e}")
            welcome_text = f"🎉 欢迎 {name} 加入本群！{count_text}"

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
        default = "🎉 欢迎 {name} 加入本群！很高兴认识你～{count_text}"
        return templates.get(group_id, self.config.get("welcome_template", default))

    async def _get_member_name(self, client, group_id: str, user_id: str) -> str:
        try:
            if not group_id.isdigit() or not user_id.isdigit(): return user_id
            res = await client.api.call_action("get_group_member_info", group_id=int(group_id), user_id=int(user_id), no_cache=True)
            return res.get("card") or res.get("nickname") or user_id
        except Exception as e:
            logger.debug(f"[group_welcome] 获取成员信息失败: {e}")
            return user_id

    async def _get_group_member_count(self, client, group_id: str):
        try:
            if not group_id.isdigit(): return None
            res = await client.api.call_action("get_group_info", group_id=int(group_id), no_cache=True)
            return res.get("member_count")
        except Exception: return None

    async def _send_group_welcome(self, client, group_id: str, user_id: str, text: str):
        try:
            if not group_id.isdigit() or not user_id.isdigit(): return
            message = [{"type": "at", "data": {"qq": user_id}}, {"type": "text", "data": {"text": f" {text}"}}]
            await client.api.call_action("send_group_msg", group_id=int(group_id), message=message)
        except Exception as e: logger.error(f"[group_welcome] 发送欢迎语异常: {e}")

    async def _send_private_rules(self, client, user_id: str):
        await asyncio.sleep(2)
        rules = self.config.get("group_rules", "📋 请遵守群规，友善交流！")
        try:
            if not user_id.isdigit(): return
            await client.api.call_action("send_private_msg", user_id=int(user_id), message=rules)
        except Exception as e:
            logger.debug(f"[group_welcome] 私聊发送群规失败: {e}")

    async def _gen_ai_welcome(self, name: str) -> str:
        try:
            provider = self.context.get_using_provider()
            if not provider: return ""
            prompt_fmt = self.config.get("ai_welcome_prompt", "请根据以下昵称，生成一句简短、温暖、有趣的入群欢迎语：{name}")
            
            try:
                final_prompt = prompt_fmt.format(name=name)
            except Exception as e:
                logger.warning(f"[group_welcome] AI 提示词模板格式错误: {e}")
                final_prompt = f"请根据以下昵称，生成一句简短、温暖、有趣的入群欢迎语：{name}"

            resp = await provider.text_chat(prompt=final_prompt, session_id=f"gw_{name}")
            return resp.completion_text.strip()
        except Exception as e:
            logger.warning(f"[group_welcome] AI 生成失败: {e}")
            return ""

    # ──────────────────────────────────────────
    # 配置辅助
    # ──────────────────────────────────────────
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
    # 指令
    # ──────────────────────────────────────────

    @filter.command_group("welcome")
    async def welcome(self, event: AstrMessageEvent):
        """指令入口。"""
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
    async def set_group_template(self, event: AstrMessageEvent):
        """设置欢迎语。"""
        raw_msg = event.message_obj.message_str.strip()
        parts = raw_msg.split(maxsplit=2)
        text_content = parts[2].strip() if len(parts) > 2 else ""
        text = text_content.replace("｛", "{").replace("｝", "}")
        
        current_group_id = str(event.message_obj.group_id) if event.message_obj.group_id else ""
        
        target_group_id = current_group_id
        final_content = text
        op_type = "set"

        if not current_group_id:
            sub_parts = text.split(maxsplit=1)
            first_word = sub_parts[0] if sub_parts else ""
            
            if first_word.isdigit():
                target_group_id = first_word
                remaining = sub_parts[1].strip() if len(sub_parts) > 1 else ""
                if remaining in ["reset", "show"]:
                    op_type = remaining
                else:
                    final_content = remaining
            elif first_word in ["reset", "show"]:
                op_type = first_word
                if len(sub_parts) < 2:
                    yield event.plain_result(f"❌ 私聊请指定群号，例如：/welcome set {first_word} 123456")
                    return
                target_group_id = sub_parts[1].strip()
            else:
                yield event.plain_result("❌ 私聊模式请先写群号或操作(reset/show)。")
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
        action = action.strip().lower()
        group_id = group_id.strip()
        
        if action == "list":
            content = "、".join(sorted(self._whitelist)) if self._whitelist else "空"
            yield event.plain_result(f"📋 白名单：{content}")
        elif action == "add" and group_id:
            self._whitelist.add(group_id)
            self._save_lists()
            yield event.plain_result(f"✅ 已加入白名单 {group_id}")
        elif action == "del" and group_id:
            self._whitelist.discard(group_id)
            self._save_lists()
            yield event.plain_result(f"✅ 已移除白名单 {group_id}")
        else:
            yield event.plain_result("用法：\n/welcome wl add <群号>\n/welcome wl del <群号>\n/welcome wl list")

    @welcome.command("bl")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def manage_blacklist(self, event: AstrMessageEvent, action: str = "", group_id: str = ""):
        action = action.strip().lower()
        group_id = group_id.strip()

        if action == "list":
            content = "、".join(sorted(self._blacklist)) if self._blacklist else "空"
            yield event.plain_result(f"🚫 黑名单：{content}")
        elif action == "add" and group_id:
            self._blacklist.add(group_id)
            self._save_lists()
            yield event.plain_result(f"✅ 已加入黑名单 {group_id}")
        elif action == "del" and group_id:
            self._blacklist.discard(group_id)
            self._save_lists()
            yield event.plain_result(f"✅ 已移除黑名单 {group_id}")
        else:
            yield event.plain_result("用法：\n/welcome bl add <群号>\n/welcome bl del <群号>\n/welcome bl list")

    @welcome.command("status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def show_status(self, event: AstrMessageEvent, target_group: str = ""):
        target_group = target_group.strip()
        curr_gid = str(event.message_obj.group_id) if event.message_obj.group_id else ""
        query_gid = target_group if target_group else curr_gid

        templates = self._load_group_templates()
        wl = "、".join(sorted(self._whitelist)) if self._whitelist else "（空）"
        bl = "、".join(sorted(self._blacklist)) if self._blacklist else "（空）"
        
        if query_gid:
            source = "群专属" if query_gid in templates else "全局默认"
            tip = f"📌 群 {query_gid} 欢迎语 [{source}]：\n{self._get_welcome_template(query_gid)}"
        else:
            tip = f"📌 已自定义群数：{len(templates)}\n💡 提示：私聊可带群号查询。"

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
{tip}"""
        yield event.plain_result(result)
