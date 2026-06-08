import time
from collections import deque

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register


@register("astrbot_plugin_LLMTempBan", "204343414", "llm临时拉黑屏蔽工具（增强版：自动反刷屏+图片去重优化）", "2.1.1")
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

        logger.info("拉黑插件初始化完成（增强版 v2.1.1）")
        logger.info(f"管理员保护列表: {self.administrators}")
        logger.info(f"默认拉黑时长: {self.default_blacklist_duration} 分钟")
        logger.info(f"已读不回冷却: {self.ignore_cooldown} 秒")
        logger.info(f"自动反刷屏: {'启用' if self.enable_auto_spam_blacklist else '禁用'} | 窗口{self.spam_window_seconds}s | 阈值{self.spam_threshold} | 自动拉黑{self.auto_blacklist_duration_minutes}min")

    # ==================== 监听钩子（关键！确保插件在消息处理流程中激活） ====================
    @filter.event_message_type(filter.EventMessageType.ALL, priority=150)
    async def on_all_message_listener(self, event: AstrMessageEvent):
        """监听所有消息的钩子（监听说话/事件监听器）。
        
        这是为了让插件在 AstrBot 的“行为”/pipeline 视图和消息处理流程中正确注册和显示。
        参考 recall_cancel 和官方 blacklist 插件的做法，使用 event_message_type.ALL + 高优先级。
        
        这里不做重逻辑（避免影响非主动消息），实际的刷屏检测、图片去重计算、自动拉黑和 stop_event 都放在 on_llm_request 中，
        因为我们只针对“主动向bot发消息”（会触发 LLM 的那些）进行计数和拦截，防止烧 token。
        
        如果未来需要早期计数所有消息，可以在这里调用部分 tracker 更新逻辑。
        """
        # 占位：确保监听钩子存在，让插件“工作”和可见。
        # 可选：在这里做一些超早期过滤（例如如果想对所有消息都计数），但按当前需求保留给 on_llm_request。
        pass

    # ==================== 内部工具方法 ====================

    def _get_bot_id(self, event: AstrMessageEvent):
        if not self.bot_id:
            raw_bot_id = event.message_obj.self_id
            self.bot_id = self._normalize_user_id(raw_bot_id)
            logger.info(f"Bot ID: {self.bot_id}")
            # Bot自己也加入保护列表，防止把自己拉黑
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
        """检查用户是否受保护（管理员/Bot自身不能被拉黑）"""
        return user_id in self.administrators

    def _extract_at_target(self, message_chain, bot_id):
        """从消息中提取@的目标用户（排除@Bot和@全体）"""
        for component in message_chain:
            if isinstance(component, At):
                if component.qq == "all":
                    continue
                at_id = self._normalize_user_id(component.qq)
                if at_id != bot_id:
                    return at_id
        return ""

    def _get_image_identifier(self, img: Image) -> str | None:
        """获取图片唯一标识（优先file，其次url），用于同一张图片去重优化。
        这是最高效的方式：纯字符串比较，无需下载或计算哈希。
        QQ等平台 file 字段通常已是唯一标识。
        """
        if not img:
            return None
        for field in ("file", "url", "path", "file_id"):
            val = getattr(img, field, None)
            if isinstance(val, str) and val.strip():
                cleaned = val.strip()
                if len(cleaned) > 4 and (cleaned.startswith(("http", "file", "base64")) or "/" in cleaned or cleaned.startswith("[")):
                    return cleaned
        return None

    def _check_and_handle_spam(self, user_id: str, event: AstrMessageEvent) -> bool:
        """检查是否触发自动刷屏拉黑。
        返回 True 表示已拦截并停止本次 LLM 调用。
        仅在 on_llm_request 中调用，因为只针对主动触发 bot 的消息（会到达 LLM 阶段的）。
        参考 recall_cancel 的尽早 stop_event 风格。
        支持图片同一张简化计数。
        """
        if not self.enable_auto_spam_blacklist:
            return False
        if self._is_protected(user_id):
            return False

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

        # spam 积分规则（简化多张相同图片）：
        # 基础 1 + 每独特图片 +1 + 重复惩罚
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
                f"[LLMTempBan] 自动触发刷屏拉黑 user={user_id} | "
                f"窗口内积分={count} >= 阈值{self.spam_threshold} | "
                f"本消息: 图片数={num_images} 独特={num_unique} 有重复={has_dup_in_msg} | "
                f"拉黑 {duration} 分钟（至 {time.ctime(unblock_time)}）。"
                f"（已参考 recall_cancel 风格在 on_llm_request 尽早 stop_event 避免烧 token）"
            )
            event.stop_event()
            return True

        return False

    # ==================== 请求拦截 ====================

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """在LLM处理前拦截：黑名单检查 + 已读不回冷却 + 自动刷屏检测。
        这里是核心，因为只有到达这里的才是“主动向bot发消息”会烧token的。
        """
        self._get_bot_id(event)
        user_id = self._normalize_user_id(event.message_obj.sender.user_id)
        session_id = self._get_session_id(event)

        # === 1. 自动刷屏检测（尽早拦截） ===
        if self._check_and_handle_spam(user_id, event):
            return

        # 2. 黑名单拦截（持续发就延长拉黑时间）
        if user_id in self.temporary_blacklist:
            unblock_time = self.temporary_blacklist[user_id]
            if time.time() < unblock_time:
                # 触发拉黑后还在发消息 → 继续增大拉黑时间（惩罚机制）
                extend_min = max(1, self.default_blacklist_duration // 2 or 5)
                new_unblock = time.time() + extend_min * 60
                self.temporary_blacklist[user_id] = max(unblock_time, new_unblock)
                logger.info(
                    f"[LLMTempBan] 拉黑用户 {user_id} 仍在发消息，延长至 {time.ctime(self.temporary_blacklist[user_id])}"
                )
                event.stop_event()
                return
            else:
                del self.temporary_blacklist[user_id]
                logger.info(f"用户 {user_id} 拉黑已过期，自动解除")

        # 3. 已读不回冷却期检查（冷却期内直接跳过LLM，零token消耗）
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

        # 4. 注入已读不回历史（让LLM知道自己之前忽略过）
        self._inject_ignore_history(session_id, req)

    def _inject_ignore_history(self, session_id, req: ProviderRequest):
        """把已读不回历史注入system prompt，让LLM记住自己沉默过"""
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

    # ==================== LLM工具：拉黑 ====================

    @filter.llm_tool(name="add_temporary_blacklist")
    async def handle_blacklist(
        self, event: AstrMessageEvent, duration_minutes: int = None
    ):
        """临时拉黑工具。当用户恶意侮辱你、持续骚扰、恶意刷屏时，你可以调用此工具将其临时拉黑。
被拉黑的用户在指定时间内发送的任何消息都不会触发你的回复。
默认拉黑当前消息的发送者；如果消息中@了其他用户，则拉黑被@的人。
受保护的用户（管理员）无法被拉黑。
参数 duration_minutes: 拉黑时长（分钟），不传则使用默认值。"""

        bot_id = self._get_bot_id(event)
        sender_id = self._normalize_user_id(event.message_obj.sender.user_id)

        # 确定拉黑目标：有@就拉黑@的人，没有就拉黑发送者
        at_target = self._extract_at_target(event.message_obj.message, bot_id)
        target_id = at_target if at_target else sender_id

        # 保护检查
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

    # ==================== LLM工具：已读不回 ====================

    @filter.llm_tool(name="read_and_ignore")
    async def handle_read_and_ignore(
        self, event: AstrMessageEvent, reason: str = "不需要回复"
    ):
        """已读不回工具。当你判断不需要回复当前消息时调用，实现真正的沉默。
适用场景：
1. 对方可能是另一个机器人，你们陷入了无意义的循环对话
2. 对方反复发送重复、无意义的内容（含图片spam）
3. 当前对话已自然结束，继续回复只会没完没了
4. 对方的消息不需要任何回应（纯表情、无意义复读等）
调用后你不会发送任何回复，且短时间内后续消息也会自动忽略（不消耗token）。
想恢复对话时调用 reset_ignore_status。
参数 reason: 简述已读不回的原因。"""

        # 防止同一事件的tool loop里重复调用
        if getattr(event, "_ignore_called", False):
            return "已读不回已生效，无需重复调用。请直接结束，不要回复任何内容。"
        event._ignore_called = True

        sender_id = self._normalize_user_id(event.message_obj.sender.user_id)
        session_id = self._get_session_id(event)

        # 记录历史
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

        # 设置冷却期
        self.ignore_cooldown_until[session_id] = time.time() + self.ignore_cooldown

        count = len(self.ignore_history[session_id])
        logger.info(
            f"已读不回：session={session_id}, sender={sender_id}, "
            f"原因='{reason}', 累计{count}次, "
            f"冷却{self.ignore_cooldown}s"
        )

        event.stop_event()
        return "已读不回执行成功。请直接结束，不要回复任何内容，不要再调用任何工具。"

    # ==================== LLM工具：恢复对话 ====================

    @filter.llm_tool(name="reset_ignore_status")
    async def handle_reset_ignore(
        self, event: AstrMessageEvent, reason: str = "可以恢复正常对话了"
    ):
        """重置已读不回状态。当你认为可以恢复正常对话时调用此工具。
调用后清除本会话的已读不回历史和冷却状态，后续消息正常响应。
参数 reason: 为什么决定恢复对话。"""

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
