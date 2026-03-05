import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register


@register("astrbot_plugin_LLMTempBan", "长安某", "llm临时拉黑屏蔽工具", "1.1.0")
class BlacklistPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.temporary_blacklist = {}  # 临时黑名单：{用户ID: 解禁时间戳}
        self.ignore_history = {}  # 已读不回历史：{session_id: [记录]}
        self.ignore_cooldown_until = {}  # 已读不回冷却：{session_id: 冷却结束时间戳}

        # 从配置加载管理员列表
        self.administrators = self.config.get("administrators", [])
        self.bot_id = ""

        # 从配置加载各项时长
        self.default_blacklist_duration = self.config.get(
            "default_blacklist_duration", 5
        )
        # 已读不回后的冷却时间（秒），冷却期内后续消息直接跳过LLM不消耗token
        self.ignore_cooldown = self.config.get("ignore_cooldown", 120)

        logger.info("拉黑插件初始化完成，等待消息事件触发")
        logger.info(f"初始管理员列表: {self.administrators}")
        logger.info(f"默认拉黑时长: {self.default_blacklist_duration} 分钟")
        logger.info(f"已读不回冷却时间: {self.ignore_cooldown} 秒")

    def _get_bot_id(self, event: AstrMessageEvent):
        """通过消息事件获取Bot ID"""
        if not self.bot_id:
            raw_bot_id = event.message_obj.self_id
            self.bot_id = self._normalize_user_id(raw_bot_id)
            logger.info(f"获取到Bot ID: 原始={raw_bot_id}, 规范化后={self.bot_id}")
            self._add_bot_to_administrators()
        return self.bot_id

    def _add_bot_to_administrators(self):
        """将Bot ID添加到管理员列表（去重并持久化）"""
        if self.bot_id and self.bot_id not in self.administrators:
            self.administrators.append(self.bot_id)
            logger.info(
                f"Bot ID {self.bot_id} 已添加为管理员，更新后管理员列表: {self.administrators}"
            )
            self.config["administrators"] = self.administrators
            self.config.save_config()
        elif self.bot_id:
            logger.info(f"Bot ID {self.bot_id} 已在管理员列表中")

    def _get_session_id(self, event: AstrMessageEvent):
        """获取会话ID"""
        if hasattr(event, "session_id") and event.session_id:
            return str(event.session_id)
        return self._normalize_user_id(event.message_obj.sender.user_id)

    def _inject_ignore_history(self, event: AstrMessageEvent, req: ProviderRequest):
        """将已读不回历史注入到LLM系统提示"""
        session_id = self._get_session_id(event)

        if session_id not in self.ignore_history or not self.ignore_history[session_id]:
            return

        history = self.ignore_history[session_id]
        recent = history[-5:]

        history_text = (
            f"\n\n[已读不回历史记录] 你在本会话中已经执行过 {len(history)} 次「已读不回」。"
            f"最近的记录如下：\n"
        )
        for r in recent:
            reason_text = f"，原因：{r['reason']}" if r.get("reason") else ""
            history_text += (
                f"  - {r['time_str']} 对用户 {r['sender_id']} 已读不回{reason_text}\n"
            )
        history_text += (
            "你可以根据当前情况自行决定：\n"
            "  - 继续已读不回 → 调用 read_and_ignore\n"
            "  - 恢复正常对话 → 调用 reset_ignore_status 清除历史后正常回复\n"
        )

        if req.system_prompt:
            req.system_prompt += history_text
        else:
            req.system_prompt = history_text

        logger.debug(
            f"已注入已读不回历史：session={session_id}, 历史次数={len(history)}"
        )

    @filter.on_llm_request()
    async def check_blacklist_before_llm(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """拦截黑名单用户的LLM请求，处理已读不回冷却"""
        self._get_bot_id(event)

        raw_user_id = event.message_obj.sender.user_id
        user_id = self._normalize_user_id(raw_user_id)
        logger.debug(
            f"检查用户LLM请求权限: 原始ID={raw_user_id}, 规范化ID={user_id}"
        )

        # 管理员不受任何限制
        if user_id in self.administrators:
            logger.debug(f"用户 {user_id} 是管理员，允许LLM请求")
            self._inject_ignore_history(event, req)
            return

        # 拦截黑名单用户
        if user_id in self.temporary_blacklist:
            unblock_time = self.temporary_blacklist[user_id]
            current_time = time.time()
            if current_time < unblock_time:
                event.stop_event()
                logger.info(
                    f"已拦截黑名单用户 {user_id} 的LLM请求（解禁时间：{time.ctime(unblock_time)}）"
                )
                return
            else:
                del self.temporary_blacklist[user_id]
                logger.info(f"用户 {user_id} 的拉黑已过期，自动移除黑名单")

        # ===== 已读不回冷却检查 =====
        # 冷却期内直接跳过LLM，不消耗token
        session_id = self._get_session_id(event)
        if session_id in self.ignore_cooldown_until:
            remaining = self.ignore_cooldown_until[session_id] - time.time()
            if remaining > 0:
                event.stop_event()
                logger.info(
                    f"会话 {session_id} 处于已读不回冷却期，直接跳过LLM（剩余 {remaining:.0f}s）"
                )
                return
            else:
                # 冷却到期，移除冷却标记，让LLM重新判断
                del self.ignore_cooldown_until[session_id]
                logger.info(
                    f"会话 {session_id} 已读不回冷却期结束，恢复LLM响应"
                )

        # 注入已读不回历史（供LLM参考决定是否继续忽略或恢复对话）
        self._inject_ignore_history(event, req)

    # ==================== 已读不回工具 ====================

    @filter.llm_tool(name="read_and_ignore")
    async def handle_read_and_ignore(
        self, event: AstrMessageEvent, reason: str = "不需要回复"
    ):
        """已读不回工具。当你认为不需要回复当前消息时，请调用此工具实现真正的「已读不回」。
适用场景包括但不限于：
1. 你发现对方可能是另一个机器人，和你陷入了无意义的循环对话；
2. 对方反复发送相似、重复、无意义的内容；
3. 你判断当前对话已经可以自然结束，继续回复只会没完没了；
4. 对方的消息确实不需要任何回应。
调用后你将不会发送任何回复，且在冷却期内后续消息也会被自动忽略，无需你反复调用。
如果之后你认为可以恢复正常对话，请调用 reset_ignore_status 工具清除历史。
参数 reason: 简要说明你选择已读不回的原因，会被记录以便后续参考。"""

        # ===== 防止同一事件的tool loop中重复调用 =====
        if getattr(event, "_read_and_ignore_called", False):
            logger.debug("read_and_ignore 在本次事件中已调用过，跳过重复执行")
            return (
                "已读不回已经生效，无需重复调用。"
                "请直接结束，不要回复任何内容，也不要调用任何工具。"
            )

        event._read_and_ignore_called = True

        sender_id = self._normalize_user_id(event.message_obj.sender.user_id)
        session_id = self._get_session_id(event)

        # 记录到已读不回历史
        if session_id not in self.ignore_history:
            self.ignore_history[session_id] = []

        self.ignore_history[session_id].append(
            {
                "timestamp": time.time(),
                "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
                "sender_id": sender_id,
                "reason": reason,
            }
        )

        # 只保留最近50条
        if len(self.ignore_history[session_id]) > 50:
            self.ignore_history[session_id] = self.ignore_history[session_id][-50:]

        # ===== 设置冷却期：冷却期内后续消息直接跳过LLM =====
        cooldown = self.ignore_cooldown
        self.ignore_cooldown_until[session_id] = time.time() + cooldown

        total_count = len(self.ignore_history[session_id])
        logger.info(
            f"Bot已读不回：session={session_id}, sender={sender_id}, "
            f"原因='{reason}', 累计忽略次数={total_count}, "
            f"冷却 {cooldown}s 至 {time.ctime(self.ignore_cooldown_until[session_id])}"
        )

        # 停止事件传播
        event.stop_event()

        return (
            "已读不回执行成功。你不会对本条消息发送任何回复，保持沉默。"
            "请直接结束，不要回复任何内容，也不要调用任何工具。"
        )

    @filter.llm_tool(name="reset_ignore_status")
    async def handle_reset_ignore(
        self, event: AstrMessageEvent, reason: str = "对话可以恢复正常"
    ):
        """重置已读不回状态工具。当你认为之前的已读不回可以解除、对话可以恢复正常时，调用此工具。
调用后会清除本会话的全部已读不回历史记录和冷却状态，后续消息将恢复正常触发你的回复。
参数 reason: 简要说明为什么决定恢复对话。"""

        session_id = self._get_session_id(event)

        cleared_count = 0
        if session_id in self.ignore_history:
            cleared_count = len(self.ignore_history[session_id])
            del self.ignore_history[session_id]

        if session_id in self.ignore_cooldown_until:
            del self.ignore_cooldown_until[session_id]

        logger.info(
            f"已读不回状态重置：session={session_id}, "
            f"清除 {cleared_count} 条记录, 原因='{reason}'"
        )

        return (
            f"已重置已读不回状态（清除了 {cleared_count} 条忽略记录），"
            f"后续消息将正常响应。你现在可以正常回复了。"
        )

    # ==================== 拉黑工具 ====================

    @filter.llm_tool(name="add_temporary_blacklist")
    async def handle_blacklist_request(
        self, event: AstrMessageEvent, duration_minutes: int = None
    ):
        """处理拉黑请求（通过事件获取Bot ID）"""
        logger.info("收到拉黑请求，开始处理...")
        bot_id = self._get_bot_id(event)

        raw_sender_id = event.message_obj.sender.user_id
        sender_id = self._normalize_user_id(raw_sender_id)
        logger.info(
            f"拉黑请求发送者: 原始ID={raw_sender_id}, 规范化ID={sender_id}"
        )

        target_id = self._extract_target_user(event.message_obj.message, bot_id)
        logger.info(
            f"拉黑请求目标用户: {target_id if target_id else '未指定'}"
        )

        if duration_minutes is None:
            duration_minutes = self.default_blacklist_duration
            logger.info(f"未指定拉黑时长，使用默认值: {duration_minutes} 分钟")
        else:
            logger.info(f"指定拉黑时长: {duration_minutes} 分钟")

        if sender_id in self.administrators:
            logger.info(f"发送者 {sender_id} 是管理员，执行管理员拉黑逻辑")
            await self._handle_admin_blacklist(target_id, duration_minutes)
        else:
            logger.info(f"发送者 {sender_id} 是普通用户，执行普通用户拉黑逻辑")
            await self._handle_normal_user_blacklist(
                sender_id, target_id, duration_minutes
            )

    async def auto_blacklist_by_bot(
        self, event: AstrMessageEvent, duration_minutes: int = None
    ):
        """Bot自动拉黑违规用户（需传入事件对象）"""
        logger.info("触发Bot自动拉黑逻辑...")
        self._get_bot_id(event)

        raw_target_id = event.message_obj.sender.user_id
        target_id = self._normalize_user_id(raw_target_id)
        logger.info(
            f"自动拉黑目标用户: 原始ID={raw_target_id}, 规范化ID={target_id}"
        )

        if target_id in self.administrators:
            logger.warning(
                f"拒绝自动拉黑管理员 {target_id}（管理员不受自动拉黑限制）"
            )
            return

        if duration_minutes is None:
            duration_minutes = self.default_blacklist_duration
            logger.info(
                f"未指定自动拉黑时长，使用默认值: {duration_minutes} 分钟"
            )

        self._add_to_blacklist(target_id, duration_minutes)
        logger.info(
            f"已自动拉黑违规用户 {target_id}，时长 {duration_minutes} 分钟"
            f"（解禁时间：{time.ctime(self.temporary_blacklist[target_id])}）"
        )

    async def _handle_admin_blacklist(self, target_id, duration):
        """管理员拉黑逻辑"""
        if not target_id:
            logger.warning("管理员拉黑失败：未指定目标用户（需@用户）")
            return
        if target_id in self.administrators:
            logger.warning(
                f"管理员拉黑失败：目标用户 {target_id} 是管理员（不能拉黑管理员）"
            )
            return
        if duration <= 0:
            logger.warning(
                f"管理员拉黑失败：时长 {duration} 分钟无效（必须大于0）"
            )
            return

        self._add_to_blacklist(target_id, duration)
        logger.info(
            f"管理员操作成功：用户 {target_id} 已被拉黑 {duration} 分钟"
            f"（解禁时间：{time.ctime(self.temporary_blacklist[target_id])}）"
        )

    async def _handle_normal_user_blacklist(self, sender_id, target_id, duration):
        """普通用户拉黑逻辑"""
        if not target_id:
            target_id = sender_id
            logger.info(
                f"普通用户 {sender_id} 未指定拉黑目标，默认处理为拉黑自己"
            )

        if duration <= 0:
            logger.warning(
                f"普通用户 {sender_id} 拉黑失败：时长 {duration} 分钟无效（必须大于0）"
            )
            return

        if target_id in self.administrators:
            actual_duration = max(5, duration)
            self._add_to_blacklist(sender_id, actual_duration)
            logger.info(
                f"普通用户 {sender_id} 尝试拉黑管理员 {target_id}，"
                f"已被反拉黑 {actual_duration} 分钟"
                f"（解禁时间：{time.ctime(self.temporary_blacklist[sender_id])}）"
            )
        elif target_id == sender_id:
            self._add_to_blacklist(sender_id, duration)
            logger.info(
                f"普通用户自助拉黑成功：{sender_id} 已拉黑自己 {duration} 分钟"
                f"（解禁时间：{time.ctime(self.temporary_blacklist[sender_id])}）"
            )
        else:
            logger.warning(
                f"普通用户 {sender_id} 拉黑失败：仅允许拉黑自己"
                f"（尝试拉黑他人 {target_id} 被拒绝）"
            )

    def _add_to_blacklist(self, user_id, duration_minutes):
        """添加用户到黑名单"""
        unblock_time = time.time() + duration_minutes * 60
        self.temporary_blacklist[user_id] = unblock_time
        logger.debug(f"黑名单更新：{user_id} → 解禁时间戳={unblock_time}")

    def _extract_target_user(self, message_chain, bot_id):
        """从消息链提取@的目标用户（排除@Bot自身）"""
        logger.debug("开始从消息链提取目标用户...")
        for component in message_chain:
            if isinstance(component, At):
                logger.debug(f"发现@组件：qq={component.qq}")
                if component.qq == "all":
                    logger.debug("跳过@全体成员")
                    continue
                at_id = self._normalize_user_id(component.qq)
                if at_id != bot_id:
                    logger.debug(
                        f"提取到目标用户：{at_id}（排除Bot自身 {bot_id}）"
                    )
                    return at_id
        logger.debug(
            "未从消息链中提取到有效目标用户（未@任何人或仅@了Bot）"
        )
        return ""

    def _normalize_user_id(self, user_id):
        """统一用户ID格式"""
        original = user_id
        if isinstance(user_id, int):
            normalized = str(user_id)
        elif isinstance(user_id, str):
            normalized = user_id.split("_")[-1].strip()
        else:
            normalized = str(user_id)
        logger.debug(f"用户ID规范化：原始={original} → 规范化后={normalized}")
        return normalized
