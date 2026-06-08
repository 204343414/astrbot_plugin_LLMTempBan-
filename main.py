import time
from collections import deque

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register


@register("astrbot_plugin_LLMTempBan", "204343414", "llm临时拉黑屏蔽工具（增强版：自动反刷屏+图片去重优化，仅私聊）", "2.1.2")
class BlacklistPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.temporary_blacklist = {}  # {用户ID: 解禁时间戳}
        self.ignore_history = {}  # {session_id: [记录]}
        self.ignore_cooldown_until = {}  # {session_id: 冷却结束时间戳}
        self.spam_tracker: dict[str, deque[float]] = {}  # {user_id: 最近触发时间戳队列}

        # 管理员列表（这些人不会被拉黑）
        self.administrators = self.config.get("administrators", [])
        self.bot_id = ""

        self.default_blacklist_duration = self.config.get(
            "default_blacklist_duration", 5
        )
        # 已读不回冷却时间（秒），冷却期内直接跳过LLM不烧token
        self.ignore_cooldown = self.config.get("ignore_cooldown", 120)

        # === 新增：自动反刷屏配置 ===
        self.enable_auto_spam_blacklist = self.config.get("enable_auto_spam_blacklist", True)
        self.spam_window_seconds = self.config.get("spam_window_seconds", 60)
        self.spam_threshold = max(2, self.config.get("spam_threshold", 5))
        self.auto_blacklist_duration_minutes = self.config.get("auto_blacklist_duration_minutes", 10)

        logger.info("拉黑插件初始化完成（增强版 v2.1.2 - 仅私聊生效）")
        logger.info(f"管理员保护列表: {self.administrators}")
        logger.info(f"默认拉黑时长: {self.default_blacklist_duration} 分钟")
        logger.info(f"已读不回冷却: {self.ignore_cooldown} 秒")
        logger.info(f"自动反刷屏: {'启用' if self.enable_auto_spam_blacklist else '禁用'} | 窗口{self.spam_window_seconds}s | 阈值{self.spam_threshold} | 自动拉黑{self.auto_blacklist_duration_minutes}min | 仅限私聊")

    # ==================== 监听钩子（参考 daily_headlineflag 的 catch-all 风格） ====================
    @filter.regex(r"[\s\S]*")
    async def _catch_all_for_spam(self, event: AstrMessageEvent):
        """
        捕获所有消息用于刷屏检测。
        参考 astrbot_plugin_daily_headlineflag 的 _catch_all_messages 实现：
        - 使用 @filter.regex(r"[\s\S]*") 捕获任何人说话（群聊/私聊）。
        - 不 yield 任何内容 = 不产生回复 = 不阻断其他插件和正常流程。
        - 在这里做早期 spam 检测。

        关键限制（按用户要求）：**只在私聊起作用**。
        - 通过 unified_msg_origin 判断 "FriendMessage"（私聊）。
        - 群聊消息直接放行，不计数、不拉黑。
        - 这样避免误伤群聊正常互动，只针对私聊傻13无脑发图烧 token 的场景。

        当检测到 spam 时：加入黑名单 + event.stop_event() 阻止后续 LLM / agent follow-up。
        """
        if not self.enable_auto_spam_blacklist:
            return

        # 只处理私聊（参考 daily_headlineflag 的 _is_private）
        umo = getattr(event, "unified_msg_origin", "") or ""
        if "FriendMessage" not in umo:
            return  # 群聊或其他，跳过，不干扰

        user_id = self._normalize_user_id(event.message_obj.sender.user_id)

        # 保护管理员
        if self._is_protected(user_id):
            return

        # 执行 spam 检查 + 可能 stop
        self._check_and_handle_spam_in_listener(user_id, event, umo)

        # 关键：不 yield，消息继续流转（除非上面已经 stop_event）

    def _check_and_handle_spam_in_listener(self, user_id: str, event: AstrMessageEvent, umo: str):
        """
        在监听阶段执行的 spam 检测（仅私聊）。
        逻辑与原来类似，但现在在早期 listener 中运行，能捕获 agent follow-up 消息。
        """
        now = time.time()
        if user_id not in self.spam_tracker:
            self.spam_tracker[user_id] = deque()

        tracker: deque = self.spam_tracker[user_id]
        window = self.spam_window_seconds

        # 清理过期
        while tracker and now - tracker[0] > window:
            tracker.popleft()

        # 计算本条消息的 spam 增量（图片去重优化）
        chain = getattr(event.message_obj, "message", None) or []
        image_keys = set()
        num_images = 0
        for comp in chain:
            if isinstance(comp, Image):
                num_images += 1
                key = self._get_image_identifier(comp)
                if key:
                    image_keys.add(key)

        num_unique = len(image_keys)
        has_dup_in_msg = num_images > num_unique > 0

        increment = 1 + num_unique + (1 if has_dup_in_msg else 0)
        increment = min(increment, 10)

        for _ in range(increment):
            tracker.append(now)

        count = len(tracker)
        if count >= self.spam_threshold:
            duration = self.auto_blacklist_duration_minutes
            unblock_time = time.time() + duration * 60
            self.temporary_blacklist[user_id] = unblock_time

            logger.info(
                f"[LLMTempBan] 【私聊】自动触发刷屏拉黑 user={user_id} | "
                f"窗口内积分={count} >= 阈值{self.spam_threshold} | "
                f"本消息: 图片数={num_images} 独特={num_unique} 有重复={has_dup_in_msg} | "
                f"拉黑 {duration} 分钟（至 {time.ctime(unblock_time)}）。"
                f"已在监听钩子 stop_event 阻止 agent follow-up 和 LLM。"
            )
            event.stop_event()
            return

    # ==================== 内部工具方法 ====================

    def _get_bot_id(self, event: AstrMessageEvent):
        if not self.bot_id:
            raw_bot_id = event.message_obj.self_id
            self.bot_id = self._normalize_user_id(raw_bot_id)
            logger.info(f"Bot ID: {self.bot_id}")
            if self.bot_id not in self.administrators:
                self.administrators.append(self.bot_id)
                self.config["administrators"] = self.administrators
                self.config.save_config()
        return self.bot_id

    def _get_session_id(self, event: AstrMessageEvent):
        if hasattr(event, "session_id") and event.session_id:
            return str(event.session_id)
        return self._normalize_user_id(event.message_obj.sender.user_id)

    def _normalize_user_id(self, user_id):
        if isinstance(user_id, int):
            return str(user_id)
        elif isinstance(user_id, str):
            return user_id.split("_")[-1].strip()
        return str(user_id)

    def _is_protected(self, user_id):
        return user_id in self.administrators

    def _extract_at_target(self, message_chain, bot_id):
        for component in message_chain:
            if isinstance(component, At):
                if component.qq == "all":
                    continue
                at_id = self._normalize_user_id(component.qq)
                if at_id != bot_id:
                    return at_id
        return ""

    def _get_image_identifier(self, img: Image) -> str | None:
        if not img:
            return None
        for field in ("file", "url", "path", "file_id"):
            val = getattr(img, field, None)
            if isinstance(val, str) and val.strip():
                cleaned = val.strip()
                if len(cleaned) > 4 and (cleaned.startswith(("http", "file", "base64")) or "/" in cleaned or cleaned.startswith("[")):
                    return cleaned
        return None

    # ==================== 请求拦截（保留原有黑名单检查 + 延长机制 + LLM 工具支持） ====================

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """在LLM处理前拦截黑名单（包括自动拉黑后的持续发送延长）。"""
        self._get_bot_id(event)
        user_id = self._normalize_user_id(event.message_obj.sender.user_id)
        session_id = self._get_session_id(event)

        # 黑名单拦截（持续发就延长拉黑时间）
        if user_id in self.temporary_blacklist:
            unblock_time = self.temporary_blacklist[user_id]
            if time.time() < unblock_time:
                extend_min = max(1, self.default_blacklist_duration // 2 or 5)
                new_unblock = time.time() + extend_min * 60
                self.temporary_blacklist[user_id] = max(unblock_time, new_unblock)
                logger.info(
                    f"[LLMTempBan] 拉黑用户 {user_id} 仍在发消息（on_llm_request），延长至 {time.ctime(self.temporary_blacklist[user_id])}"
                )
                event.stop_event()
                return
            else:
                del self.temporary_blacklist[user_id]
                logger.info(f"用户 {user_id} 拉黑已过期，自动解除")

        # 已读不回冷却期检查
        if session_id in self.ignore_cooldown_until:
            remaining = self.ignore_cooldown_until[session_id] - time.time()
            if remaining > 0:
                event.stop_event()
                logger.info(
                    f"会话 {session_id} 已读不回冷却中（剩余 {remaining:.0f}s），跳过LLM"
                )
                return
            else:
                del self.ignore_cooldown_until[session_id]
                logger.info(f"会话 {session_id} 冷却结束，恢复LLM响应")

        self._inject_ignore_history(session_id, req)

    def _inject_ignore_history(self, session_id, req: ProviderRequest):
        if session_id not in self.ignore_history:
            return
        history = self.ignore_history[session_id]
        if not history:
            return

        recent = history[-5:]
        text = (
            f"\n\n[已读不回记录] 你在本会话已执行 {len(history)} 次已读不回。"
            f"最近记录：\n"
        )
        for r in recent:
            text += f"  - {r['time_str']} 忽略了 {r['sender_id']}（{r['reason']}）\n"
        text += (
            "如果对方仍在骚扰/重复/无意义发言，继续调用 read_and_ignore 保持沉默。\n"
            "如果你觉得可以恢复正常对话了，调用 reset_ignore_status 清除记录。\n"
        )

        if req.system_prompt:
            req.system_prompt += text
        else:
            req.system_prompt = text

    # ==================== LLM工具（保持不变） ====================

    @filter.llm_tool(name="add_temporary_blacklist")
    async def handle_blacklist(
        self, event: AstrMessageEvent, duration_minutes: int = None
    ):
        bot_id = self._get_bot_id(event)
        sender_id = self._normalize_user_id(event.message_obj.sender.user_id)

        at_target = self._extract_at_target(event.message_obj.message, bot_id)
        target_id = at_target if at_target else sender_id

        if self._is_protected(target_id):
            logger.warning(f"拒绝拉黑受保护用户 {target_id}")
            return f"无法拉黑用户 {target_id}，该用户受保护。"

        if duration_minutes is None or duration_minutes <= 0:
            duration_minutes = self.default_blacklist_duration

        unblock_time = time.time() + duration_minutes * 60
        self.temporary_blacklist[target_id] = unblock_time

        logger.info(
            f"已拉黑用户 {target_id}，时长 {duration_minutes} 分钟"
            f"（解禁：{time.ctime(unblock_time)}）"
        )
        return (
            f"已将用户 {target_id} 临时拉黑 {duration_minutes} 分钟，"
            f"在此期间该用户的消息不会触发你的回复。"
        )

    @filter.llm_tool(name="read_and_ignore")
    async def handle_read_and_ignore(
        self, event: AstrMessageEvent, reason: str = "不需要回复"
    ):
        if getattr(event, "_ignore_called", False):
            return "已读不回已生效，无需重复调用。请直接结束，不要回复任何内容。"
        event._ignore_called = True

        sender_id = self._normalize_user_id(event.message_obj.sender.user_id)
        session_id = self._get_session_id(event)

        if session_id not in self.ignore_history:
            self.ignore_history[session_id] = []
        self.ignore_history[session_id].append({
            "timestamp": time.time(),
            "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sender_id": sender_id,
            "reason": reason,
        })
        if len(self.ignore_history[session_id]) > 50:
            self.ignore_history[session_id] = self.ignore_history[session_id][-50:]

        self.ignore_cooldown_until[session_id] = time.time() + self.ignore_cooldown

        count = len(self.ignore_history[session_id])
        logger.info(
            f"已读不回：session={session_id}, sender={sender_id}, "
            f"原因='{reason}', 累计{count}次, "
            f"冷却{self.ignore_cooldown}s"
        )

        event.stop_event()
        return "已读不回执行成功。请直接结束，不要回复任何内容，不要再调用任何工具。"

    @filter.llm_tool(name="reset_ignore_status")
    async def handle_reset_ignore(
        self, event: AstrMessageEvent, reason: str = "可以恢复正常对话了"
    ):
        session_id = self._get_session_id(event)
        cleared = 0

        if session_id in self.ignore_history:
            cleared = len(self.ignore_history[session_id])
            del self.ignore_history[session_id]
        if session_id in self.ignore_cooldown_until:
            del self.ignore_cooldown_until[session_id]

        logger.info(
            f"已读不回重置：session={session_id}, 清除{cleared}条记录, 原因='{reason}'"
        )
        return f"已重置（清除{cleared}条记录），后续消息正常响应，你现在可以正常回复了。"
