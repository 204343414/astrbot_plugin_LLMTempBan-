"""
LLMTempBan 增强版 v2.4 - 支持 LLM 工具拉黑、永久拉黑与自定义拉黑语录

核心目标：
1. 通过 LLM 工具 add_temporary_blacklist 实现灵活拉黑（5 分钟 ~ 永久）
2. 永久拉黑用户触发 Bot 时，按配置间隔自动回复自定义语录（默认 1 小时一次）
3. 确保 stop_event() 能真正阻止消息触发 LLM 调用，避免烧 token
4. 临时黑名单到点自动解除
"""

import random
import time
from collections import deque
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import At, Image
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_LLMTempBan_v2",
    "204343414",
    "LLM临时拉黑（增强版：支持永久拉黑+自定义拉黑语录）",
    "2.4.0",
)
class BlacklistPluginV2(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.temporary_blacklist = {}  # {用户ID: 解禁时间戳}
        self.permanent_ban_time = {}  # {用户ID: 拉黑时间戳}
        self.permanent_ban_last_reply = {}  # {用户ID: 上次自动回复时间戳}
        self.ignore_history = {}
        self.ignore_cooldown_until = {}
        self.spam_tracker: dict[str, deque[float]] = {}

        self.administrators = self.config.get("administrators", [])
        self.bot_id = ""

        self.default_blacklist_duration = self.config.get(
            "default_blacklist_duration", 5
        )
        self.ignore_cooldown = self.config.get("ignore_cooldown", 120)

        # 自动反刷屏配置
        self.enable_auto_spam_blacklist = self.config.get(
            "enable_auto_spam_blacklist", True
        )
        self.spam_window_seconds = self.config.get("spam_window_seconds", 60)
        self.spam_threshold = max(2, self.config.get("spam_threshold", 5))
        self.auto_blacklist_duration_minutes = self.config.get(
            "auto_blacklist_duration_minutes", 10
        )

        # 好友检测与永久拉黑自动删好友配置
        self.auto_delete_friend_on_permanent_ban = self.config.get(
            "auto_delete_friend_on_permanent_ban", False
        )
        self.friend_list_refresh_interval = self.config.get(
            "friend_list_refresh_interval", 3600
        )
        self.friend_list_cache: set[str] = set()
        self.friend_list_last_refresh = 0.0

        # 永久拉黑自动回复配置
        self.permanent_ban_messages = self.config.get(
            "permanent_ban_messages",
            [
                "您已被拉黑 {user_id}，已拉黑 {duration}。",
                "被拉黑还锲而不舍地戳 Bot，建议输入 /删除bot 或自行删除 Bot 好友，对大家都好~",
                "您已被永久拉黑，请继续表演，反正 Bot 不会再理你了。",
                "黑名单里的空气还好吗？{user_id} 同学。",
                "低质量骚扰已触发永久屏蔽，您已收获 Bot 的沉默大礼包。",
            ],
        )
        self.permanent_ban_reply_interval = self.config.get(
            "permanent_ban_reply_interval", 3600
        )

        # 好友专用永久拉黑语录（为空则使用通用语录）
        self.friend_permanent_ban_messages = self.config.get(
            "friend_permanent_ban_messages", []
        )

        # 确保是列表
        self.permanent_ban_messages = self._ensure_message_list(
            self.permanent_ban_messages
        )
        self.friend_permanent_ban_messages = self._ensure_message_list(
            self.friend_permanent_ban_messages
        )

        logger.info("=" * 60)
        logger.info("拉黑插件 v2.4.0 初始化完成")
        logger.info(f"管理员列表: {self.administrators}")
        logger.info(f"自动拉黑阈值: {self.spam_threshold}条/{self.spam_window_seconds}秒")
        logger.info(f"拉黑时长: {self.auto_blacklist_duration_minutes}分钟")
        logger.info(f"永久拉黑回复间隔: {self.permanent_ban_reply_interval}秒")
        logger.info(f"永久拉黑语录数: {len(self.permanent_ban_messages)}")
        logger.info(f"好友专用永久拉黑语录数: {len(self.friend_permanent_ban_messages)}")
        logger.info(f"永久拉黑自动删好友: {self.auto_delete_friend_on_permanent_ban}")
        logger.info("=" * 60)

    # ==================== 第一道防线：监听钩子（全局黑名单拦截 + 私聊刷屏检测） ====================
    @filter.regex(r"[\s\S]*", priority=10)
    async def _catch_all_for_spam(self, event: AstrMessageEvent):
        """全局黑名单过滤：被拉黑用户（含命令、LLM）一律 stop_event；私聊额外执行刷屏检测。"""

        user_id = self._normalize_user_id(event.message_obj.sender.user_id)

        # 保护管理员：管理员不受任何拉黑限制
        if self._is_protected(user_id):
            return

        # === 检查是否已在黑名单中 ===
        if user_id in self.temporary_blacklist:
            unblock_time = self.temporary_blacklist[user_id]
            if time.time() < unblock_time or unblock_time == float("inf"):
                # 仍在拉黑期内（含永久）
                is_permanent = unblock_time == float("inf")

                # 永久拉黑：按间隔发送自定义语录
                if is_permanent:
                    await self._send_permanent_ban_message(event, user_id)
                    logger.info(
                        f"[LLMTempBan] 永久拉黑用户触发被拦截 user={user_id}"
                    )
                else:
                    # 私聊中继续发消息则延长拉黑时间（惩罚）
                    umo = getattr(event, "unified_msg_origin", "") or ""
                    if "FriendMessage" in umo:
                        extend_min = max(1, self.default_blacklist_duration // 2 or 5)
                        new_unblock = time.time() + extend_min * 60
                        self.temporary_blacklist[user_id] = max(
                            unblock_time, new_unblock
                        )
                        logger.info(
                            f"[LLMTempBan] 【私聊】黑名单用户继续发消息 user={user_id} "
                            f"延长至 {time.ctime(self.temporary_blacklist[user_id])}"
                        )
                    else:
                        logger.info(
                            f"[LLMTempBan] 【群聊】黑名单用户触发被拦截 user={user_id}"
                        )

                # 立即 stop_event，阻止后续命令、LLM 等一切处理
                event.stop_event()
                return
            else:
                # 拉黑已过期，删除记录
                del self.temporary_blacklist[user_id]
                self.permanent_ban_time.pop(user_id, None)
                self.permanent_ban_last_reply.pop(user_id, None)
                logger.info(f"[LLMTempBan] 用户 {user_id} 拉黑已过期，自动解除")

        # === 私聊刷屏检测 ===
        umo = getattr(event, "unified_msg_origin", "") or ""
        if "FriendMessage" in umo and self.enable_auto_spam_blacklist:
            self._check_spam(user_id, event)

    def _check_spam(self, user_id: str, event: AstrMessageEvent):
        """检测刷屏并可能触发拉黑"""
        now = time.time()

        if user_id not in self.spam_tracker:
            self.spam_tracker[user_id] = deque()

        tracker = self.spam_tracker[user_id]
        window = self.spam_window_seconds

        # 清理过期记录
        while tracker and now - tracker[0] > window:
            tracker.popleft()

        # 计算本条消息的积分
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
        has_dup = num_images > num_unique > 0

        # 积分计算：基础1分 + 独特图片数 + 重复惩罚
        increment = 1 + num_unique + (1 if has_dup else 0)
        increment = min(increment, 10)

        for _ in range(increment):
            tracker.append(now)

        count = len(tracker)

        if count >= self.spam_threshold:
            # 触发拉黑
            duration = self.auto_blacklist_duration_minutes
            unblock_time = time.time() + duration * 60
            self.temporary_blacklist[user_id] = unblock_time

            logger.warning(
                f"[LLMTempBan] ⛔ 【私聊】自动触发刷屏拉黑 user={user_id}\n"
                f" 窗口内积分: {count} >= 阈值 {self.spam_threshold}\n"
                f" 本消息: 图片数={num_images} 独特={num_unique} 有重复={has_dup}\n"
                f" 拉黑 {duration} 分钟（至 {time.ctime(unblock_time)}）"
            )

            # ⭐ 关键：立即stop_event，阻止所有后续处理
            event.stop_event()

            # 清空该用户的刷屏积分
            self.spam_tracker[user_id].clear()

            return

    # ==================== 第二道防线：on_llm_request 钩子（最终拦截） ====================
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        LLM请求前的最终拦截点。
        这是防止消息触发LLM的最后一道防线。
        """
        # 初始化bot_id
        self._get_bot_id(event)

        user_id = self._normalize_user_id(event.message_obj.sender.user_id)
        session_id = self._get_session_id(event)

        # === 最终黑名单检查 ===
        if user_id in self.temporary_blacklist:
            unblock_time = self.temporary_blacklist[user_id]
            current_time = time.time()

            if current_time < unblock_time or unblock_time == float("inf"):
                # 仍在拉黑期
                remaining = (
                    unblock_time - current_time
                    if unblock_time != float("inf")
                    else float("inf")
                )
                logger.info(
                    f"[LLMTempBan] 🚫 on_llm_request 拦截 user={user_id} "
                    f"剩余 {remaining:.0f}秒"
                )

                # 永久拉黑：按间隔发送自定义语录
                if unblock_time == float("inf"):
                    await self._send_permanent_ban_message(event, user_id)

                # ⭐ 关键：确保stop_event，阻止LLM调用
                event.stop_event()
                return
            else:
                # 已过期，删除记录
                del self.temporary_blacklist[user_id]
                self.permanent_ban_time.pop(user_id, None)
                self.permanent_ban_last_reply.pop(user_id, None)
                logger.info(f"[LLMTempBan] 用户 {user_id} 拉黑已过期")

        # === 已读不回冷却检查 ===
        if session_id in self.ignore_cooldown_until:
            remaining = self.ignore_cooldown_until[session_id] - time.time()
            if remaining > 0:
                logger.info(
                    f"[LLMTempBan] 已读不回冷却中 session={session_id} 剩余 {remaining:.0f}s"
                )
                event.stop_event()
                return
            else:
                del self.ignore_cooldown_until[session_id]

        # 注入已读不回历史
        self._inject_ignore_history(session_id, req)

    def _inject_ignore_history(self, session_id, req: ProviderRequest):
        """注入已读不回历史到请求中"""
        if session_id not in self.ignore_history or not self.ignore_history[session_id]:
            return

        history = self.ignore_history[session_id][-5:]
        text = (
            f"\n\n[已读不回记录] 你在本会话已执行 {len(self.ignore_history[session_id])} 次已读不回。\n"
        )
        for r in history:
            text += f" - {r['time_str']} 忽略了 {r['sender_id']}（{r['reason']}）\n"
        text += "如果对方仍在骚扰，继续调用 read_and_ignore 保持沉默。"

        try:
            from astrbot.core.agent.message import TextPart

            req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())
        except Exception as e:
            logger.debug(f"注入已读不回历史失败: {e}")

    # ==================== 永久拉黑自动回复 ====================
    async def _send_permanent_ban_message(self, event: AstrMessageEvent, user_id: str):
        """按配置间隔向永久拉黑用户发送自定义语录；如目标是好友且开启，可优先使用好友专用语录。"""
        now = time.time()
        last_reply = self.permanent_ban_last_reply.get(user_id, 0)

        if now - last_reply < self.permanent_ban_reply_interval:
            return

        # 选择语录池：好友优先使用 friend_permanent_ban_messages（如果配置且非空）
        messages = self.permanent_ban_messages
        if (
            self.friend_permanent_ban_messages
            and await self._is_friend(user_id)
        ):
            messages = self.friend_permanent_ban_messages

        if not messages:
            return

        template = random.choice(messages)
        ban_time = self.permanent_ban_time.get(user_id, now)
        message = self._render_message(template, user_id, ban_time)

        try:
            await event.send(event.plain_result(message))
            self.permanent_ban_last_reply[user_id] = now
            logger.info(
                f"[LLMTempBan] 已向永久拉黑用户 {user_id} 发送语录: {message[:50]}..."
            )
        except Exception as e:
            logger.warning(f"[LLMTempBan] 发送永久拉黑语录失败: {e}")

    def _render_message(self, template: str, user_id: str, ban_time: float) -> str:
        """渲染语录模板变量"""
        dt = datetime.fromtimestamp(ban_time)
        return (
            template.replace("{user_id}", user_id)
            .replace("{duration}", "永久")
            .replace("{ban_time}", dt.strftime("%Y-%m-%d %H:%M"))
        )

    # ==================== 好友检测与自动删好友 ====================
    async def _get_client(self):
        """获取可用的 aiocqhttp 客户端（参考 HappyBirthday 插件）"""
        try:
            platforms = self.context.platform_manager.get_insts()
            for platform in platforms:
                if hasattr(platform, "get_client"):
                    client = platform.get_client()
                    if client:
                        return client
        except Exception as e:
            logger.debug(f"[LLMTempBan] 获取平台客户端失败: {e}")
        return None

    async def _refresh_friend_list(self):
        """刷新并缓存好友列表"""
        now = time.time()
        if now - self.friend_list_last_refresh < self.friend_list_refresh_interval:
            return

        client = await self._get_client()
        if not client:
            return

        try:
            friends = await client.get_friend_list()
            self.friend_list_cache = {
                self._normalize_user_id(str(f.get("user_id", "")))
                for f in friends
                if f.get("user_id")
            }
            self.friend_list_last_refresh = now
            logger.info(
                f"[LLMTempBan] 刷新好友列表成功，共 {len(self.friend_list_cache)} 人"
            )
        except Exception as e:
            logger.warning(f"[LLMTempBan] 刷新好友列表失败: {e}")

    async def _is_friend(self, user_id: str) -> bool:
        """检查用户是否在 Bot 好友列表中"""
        await self._refresh_friend_list()
        return user_id in self.friend_list_cache

    async def _delete_friend(self, user_id: str) -> bool:
        """尝试删除好友，兼容常见 OneBot 实现"""
        client = await self._get_client()
        if not client:
            return False

        try:
            # 尝试直接调用 delete_friend
            if hasattr(client, "delete_friend"):
                await client.delete_friend(user_id=int(user_id))
                logger.info(f"[LLMTempBan] 已删除好友 {user_id}")
                return True
        except Exception as e:
            logger.debug(f"[LLMTempBan] delete_friend 失败: {e}")

        try:
            # 回退到 call_action
            await client.call_action("delete_friend", user_id=int(user_id))
            logger.info(f"[LLMTempBan] 已通过 call_action 删除好友 {user_id}")
            return True
        except Exception as e:
            logger.warning(f"[LLMTempBan] 删除好友 {user_id} 失败: {e}")

        return False

    # ==================== 命令处理 ====================
    @filter.command("拉黑_")
    async def ban_user(self, event: AstrMessageEvent, target: str = None):
        """拉黑用户（管理员）"""
        self._get_bot_id(event)
        user_id = self._normalize_user_id(event.message_obj.sender.user_id)

        if not self._is_protected(user_id):
            yield event.plain_result("只有管理员可以使用此命令")
            return

        if not target:
            yield event.plain_result("请指定要拉黑的用户：/拉黑_@用户")
            return

        target_id = self._extract_target_id(target)
        if not target_id:
            yield event.plain_result("无法识别目标用户")
            return

        if self._is_protected(target_id):
            yield event.plain_result("无法拉黑管理员")
            return

        duration = self.default_blacklist_duration
        unblock_time = time.time() + duration * 60
        self.temporary_blacklist[target_id] = unblock_time

        logger.warning(f"[LLMTempBan] 管理员 {user_id} 拉黑用户 {target_id} {duration} 分钟")

        yield event.plain_result(
            f"已拉黑用户 {target_id} {duration} 分钟\n"
            f"到期时间: {time.ctime(unblock_time)}\n"
            f"拉黑期间对方消息不会触发LLM回复"
        )

    @filter.command("解禁_")
    async def unban_user(self, event: AstrMessageEvent, target: str = None):
        """解禁用户"""
        self._get_bot_id(event)
        user_id = self._normalize_user_id(event.message_obj.sender.user_id)

        if not self._is_protected(user_id):
            yield event.plain_result("只有管理员可以使用此命令")
            return

        if not target:
            yield event.plain_result("请指定要解禁的用户")
            return

        target_id = self._extract_target_id(target)
        if target_id in self.temporary_blacklist:
            del self.temporary_blacklist[target_id]
            self.permanent_ban_time.pop(target_id, None)
            self.permanent_ban_last_reply.pop(target_id, None)
            logger.info(f"[LLMTempBan] 管理员 {user_id} 解禁用户 {target_id}")
            yield event.plain_result(f"已解禁用户 {target_id}")
        else:
            yield event.plain_result(f"用户 {target_id} 不在黑名单中")

    @filter.command("拉黑列表_")
    async def list_banned(self, event: AstrMessageEvent):
        """查看当前拉黑列表"""
        self._get_bot_id(event)

        if not self.temporary_blacklist:
            yield event.plain_result("当前没有拉黑用户 ✅")
            return

        now = time.time()
        lines = ["📋 当前拉黑列表：\n"]
        for uid, unblock_time in list(self.temporary_blacklist.items()):
            remaining = unblock_time - now
            if unblock_time == float("inf"):
                lines.append(f"• {uid}: 永久拉黑")
            elif remaining > 0:
                mins = int(remaining // 60)
                secs = int(remaining % 60)
                lines.append(f"• {uid}: 剩余 {mins}分{secs}秒")
            else:
                lines.append(f"• {uid}: 已过期（即将自动解除）")
                del self.temporary_blacklist[uid]
                self.permanent_ban_time.pop(uid, None)
                self.permanent_ban_last_reply.pop(uid, None)

        yield event.plain_result("\n".join(lines))

    @filter.command("我的拉黑_")
    async def self_ban(self, event: AstrMessageEvent):
        """拉黑自己"""
        user_id = self._normalize_user_id(event.message_obj.sender.user_id)
        duration = self.default_blacklist_duration
        unblock_time = time.time() + duration * 60
        self.temporary_blacklist[user_id] = unblock_time
        yield event.plain_result(
            f"已拉黑自己 {duration} 分钟\n" f"到期时间: {time.ctime(unblock_time)}"
        )

    @filter.command("解除拉黑_")
    async def self_unban(self, event: AstrMessageEvent):
        """解除自己的拉黑"""
        user_id = self._normalize_user_id(event.message_obj.sender.user_id)
        if user_id in self.temporary_blacklist:
            del self.temporary_blacklist[user_id]
            self.permanent_ban_time.pop(user_id, None)
            self.permanent_ban_last_reply.pop(user_id, None)
            yield event.plain_result("已解除自拉黑")
        else:
            yield event.plain_result("你没有被拉黑")

    # ==================== LLM 工具 ====================
    @filter.llm_tool(name="add_temporary_blacklist")
    async def add_temporary_blacklist(
        self,
        event: AstrMessageEvent,
        duration_minutes: int,
        target_user_id: str = "",
        reason: str = "",
    ):
        '''拉黑工具。管理员可拉黑任意非管理员用户；普通用户只能拉黑自己；尝试拉黑管理员会被反向拉黑。
        duration_minutes=-1 表示永久拉黑。消息中带 @ 时优先以 @ 目标为准。

        Args:
            duration_minutes(int): 拉黑时长（分钟），-1 表示永久拉黑
            target_user_id(string): 目标用户 ID，可选；群聊中如消息 @ 了用户则优先使用 @ 目标
            reason(string): 拉黑原因，可选
        '''
        self._get_bot_id(event)
        caller_id = self._normalize_user_id(event.message_obj.sender.user_id)

        # 优先从消息链的 @ 组件提取目标
        at_target = self._extract_at_target_from_event(event)
        if at_target:
            target_id = self._normalize_user_id(at_target)
        elif target_user_id:
            target_id = self._normalize_user_id(target_user_id)
        else:
            target_id = caller_id

        # 管理员权限：必须明确指定目标（@ 或 target_user_id）
        if self._is_protected(caller_id):
            if not at_target and not target_user_id:
                return "请指定要拉黑的目标用户（@ 目标或提供 target_user_id）。"
            if self._is_protected(target_id):
                return f"无法拉黑管理员 {target_id}。"

            return await self._ban_user(
                target_id, duration_minutes, caller=caller_id, reason=reason
            )

        # 普通用户只能拉黑自己
        if target_id != caller_id:
            if self._is_protected(target_id):
                # 尝试拉黑管理员 → 反向拉黑自己，至少 5 分钟
                return await self._ban_user(
                    caller_id,
                    max(5, duration_minutes if duration_minutes > 0 else 5),
                    caller="system",
                    reason="尝试拉黑管理员，被反向拉黑",
                )
            return "普通用户只能拉黑自己，无法拉黑其他用户。"

        return await self._ban_user(
            caller_id, duration_minutes, caller=caller_id, reason=reason
        )

    @filter.llm_tool(name="read_and_ignore")
    async def read_and_ignore(self, event: AstrMessageEvent, reason: str = "无意义发言"):
        '''已读不回工具。

        Args:
            reason(string): 忽略原因
        '''
        session_id = self._get_session_id(event)
        user_id = self._normalize_user_id(event.message_obj.sender.user_id)

        if session_id not in self.ignore_history:
            self.ignore_history[session_id] = []

        self.ignore_history[session_id].append(
            {
                "time_str": time.strftime("%Y-%m-%d %H:%M"),
                "sender_id": user_id,
                "reason": reason,
            }
        )

        self.ignore_cooldown_until[session_id] = time.time() + self.ignore_cooldown
        logger.info(f"[LLMTempBan] 已读不回 session={session_id}")

        return "已忽略此消息。"

    @filter.llm_tool(name="reset_ignore_status")
    async def reset_ignore_status(self, event: AstrMessageEvent):
        '''重置已读不回状态'''
        session_id = self._get_session_id(event)

        if session_id in self.ignore_history:
            del self.ignore_history[session_id]
        if session_id in self.ignore_cooldown_until:
            del self.ignore_cooldown_until[session_id]

        return "已重置状态。"

    # ==================== 工具方法 ====================
    def _ensure_message_list(self, value) -> list[str]:
        """确保配置项是字符串列表"""
        if not value:
            return []
        if not isinstance(value, list):
            return [str(value).strip()] if str(value).strip() else []
        return [str(m).strip() for m in value if str(m).strip()]

    async def _ban_user(
        self, target_id: str, duration_minutes: int, caller: str, reason: str = ""
    ) -> str:
        """执行拉黑，并返回结果描述。永久拉黑时可选择自动删除好友。"""
        now = time.time()
        permanent = duration_minutes == -1

        if permanent:
            unblock_time = float("inf")
            duration_text = "永久"
        elif duration_minutes > 0:
            unblock_time = now + duration_minutes * 60
            duration_text = f"{duration_minutes} 分钟"
        else:
            # 0 或未指定时，使用默认拉黑时长
            duration_minutes = self.default_blacklist_duration
            unblock_time = now + duration_minutes * 60
            duration_text = f"{duration_minutes} 分钟"

        self.temporary_blacklist[target_id] = unblock_time
        if permanent:
            self.permanent_ban_time[target_id] = now
            self.permanent_ban_last_reply[target_id] = 0

        log_reason = f" 原因: {reason}" if reason else ""
        logger.warning(
            f"[LLMTempBan] 管理员/LLM {caller} 拉黑用户 {target_id} {duration_text}{log_reason}"
        )

        extra_text = ""
        if permanent and self.auto_delete_friend_on_permanent_ban:
            try:
                if await self._is_friend(target_id):
                    deleted = await self._delete_friend(target_id)
                    if deleted:
                        extra_text = " 已自动从 Bot 好友列表中删除该用户。"
                    else:
                        extra_text = " 尝试自动删除好友失败，请手动处理。"
                else:
                    extra_text = " 该用户不是 Bot 好友，无需删除。"
            except Exception as e:
                logger.warning(f"[LLMTempBan] 永久拉黑好友检测/删除失败: {e}")
                extra_text = " 好友检测/删除过程出错。"

        if permanent:
            return (
                f"已永久拉黑用户 {target_id}。"
                f"此后该用户每次触发 Bot，将每隔 {self.permanent_ban_reply_interval} 秒"
                f"收到一条自动回复语录。{extra_text}"
            )
        return (
            f"已拉黑用户 {target_id} {duration_text}，"
            f"到期时间: {time.ctime(unblock_time)}。"
            f"拉黑期间对方消息不会触发 LLM 回复。"
        )

    def _get_bot_id(self, event: AstrMessageEvent):
        if not self.bot_id:
            self.bot_id = self._normalize_user_id(event.message_obj.self_id)
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

    def _extract_at_target_from_event(self, event: AstrMessageEvent) -> str:
        """从消息链中的 At 组件提取目标用户 ID"""
        chain = getattr(event.message_obj, "message", None) or []
        for comp in chain:
            if isinstance(comp, At):
                target = getattr(comp, "target", None) or getattr(comp, "qq", None)
                if target is not None:
                    return str(target)
        # 兼容 CQ:at,qq=xxx 的字符串 fallback（部分适配器可能不在 chain 中）
        raw = str(getattr(event.message_obj, "raw_message", ""))
        import re

        at_match = re.search(r"CQ:at,qq=(\d+)", raw)
        if at_match:
            return at_match.group(1)
        return ""

    def _extract_target_id(self, target: str) -> str:
        import re

        at_match = re.search(r"CQ:at,qq=(\d+)", target)
        if at_match:
            return at_match.group(1)
        num_match = re.search(r"(\d{5,})", target)
        if num_match:
            return num_match.group(1)
        return ""

    def _get_image_identifier(self, img: Image) -> str | None:
        if not img:
            return None
        for field in ("file", "url", "path", "file_id"):
            val = getattr(img, field, None)
            if isinstance(val, str) and val.strip():
                cleaned = val.strip()
                if len(cleaned) > 4 and (
                    cleaned.startswith(("http", "file", "base64"))
                    or "/" in cleaned
                    or cleaned.startswith("[")
                ):
                    return cleaned
        return None

    async def terminate(self):
        """插件卸载时保存配置"""
        self.config.save_config()
