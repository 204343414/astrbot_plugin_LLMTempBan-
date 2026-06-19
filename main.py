"""
LLMTempBan 增强版 v2 - 确保 stop_event() 阻止LLM + 临时黑名单自动过期

核心目标：
1. 确保 stop_event() 能真正阻止消息触发LLM调用
2. 临时黑名单到点自动解除
3. 配合 AstrBot 内置速率限制作为补充
"""

import time
from collections import deque

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import At, Image
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

@register("astrbot_plugin_LLMTempBan_v2", "204343414", 
          "LLM临时拉黑（增强版：确保stop_event阻止LLM+临时黑名单自动过期，仅私聊）", "2.3.0")
class BlacklistPluginV2(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.temporary_blacklist = {}  # {用户ID: 解禁时间戳}
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
        self.enable_auto_spam_blacklist = self.config.get("enable_auto_spam_blacklist", True)
        self.spam_window_seconds = self.config.get("spam_window_seconds", 60)
        self.spam_threshold = max(2, self.config.get("spam_threshold", 5))
        self.auto_blacklist_duration_minutes = self.config.get("auto_blacklist_duration_minutes", 10)
        
        logger.info("=" * 60)
        logger.info("拉黑插件 v2.3.0 初始化完成")
        logger.info(f"管理员列表: {self.administrators}")
        logger.info(f"自动拉黑阈值: {self.spam_threshold}条/{self.spam_window_seconds}秒")
        logger.info(f"拉黑时长: {self.auto_blacklist_duration_minutes}分钟")
        logger.info("=" * 60)

    # ==================== 第一道防线：监听钩子（早期拦截刷屏） ====================
    @filter.regex(r'[\s\S]*')
    async def _catch_all_for_spam(self, event: AstrMessageEvent):
        """捕获所有消息用于刷屏检测和黑名单过滤"""
        
        # 只处理私聊
        umo = getattr(event, "unified_msg_origin", "") or ""
        if "FriendMessage" not in umo:
            return
        
        user_id = self._normalize_user_id(event.message_obj.sender.user_id)
        
        # 保护管理员
        if self._is_protected(user_id):
            return
        
        # === 检查是否已在黑名单中 ===
        if user_id in self.temporary_blacklist:
            unblock_time = self.temporary_blacklist[user_id]
            if time.time() < unblock_time:
                # 仍在拉黑期内，延长拉黑时间
                extend_min = max(1, self.default_blacklist_duration // 2 or 5)
                new_unblock = time.time() + extend_min * 60
                self.temporary_blacklist[user_id] = max(unblock_time, new_unblock)
                
                logger.info(
                    f"[LLMTempBan] 【私聊】黑名单用户继续发消息 user={user_id} "
                    f"延长至 {time.ctime(self.temporary_blacklist[user_id])}"
                )
                
                # 立即 stop_event，阻止消息继续传递到LLM
                event.stop_event()
                return
            else:
                # 拉黑已过期，删除记录
                del self.temporary_blacklist[user_id]
                logger.info(f"[LLMTempBan] 用户 {user_id} 拉黑已过期，自动解除")
        
        # === 执行刷屏检测 ===
        if self.enable_auto_spam_blacklist:
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
                f"  窗口内积分: {count} >= 阈值 {self.spam_threshold}\n"
                f"  本消息: 图片数={num_images} 独特={num_unique} 有重复={has_dup}\n"
                f"  拉黑 {duration} 分钟（至 {time.ctime(unblock_time)}）"
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
            
            if current_time < unblock_time:
                # 仍在拉黑期
                remaining = unblock_time - current_time
                logger.info(
                    f"[LLMTempBan] 🚫 on_llm_request 拦截 user={user_id} "
                    f"剩余 {remaining:.0f}秒"
                )
                
                # ⭐ 关键：确保stop_event，阻止LLM调用
                event.stop_event()
                return
            else:
                # 已过期，删除记录
                del self.temporary_blacklist[user_id]
                logger.info(f"[LLMTempBan] 用户 {user_id} 拉黑已过期")
        
        # === 已读不回冷却检查 ===
        if session_id in self.ignore_cooldown_until:
            remaining = self.ignore_cooldown_until[session_id] - time.time()
            if remaining > 0:
                logger.info(f"[LLMTempBan] 已读不回冷却中 session={session_id} 剩余 {remaining:.0f}s")
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

    # ==================== 命令处理 ====================
    @filter.command("拉黑")
    async def ban_user(self, event: AstrMessageEvent, target: str = None):
        """拉黑用户（管理员）"""
        self._get_bot_id(event)
        user_id = self._normalize_user_id(event.message_obj.sender.user_id)
        
        if not self._is_protected(user_id):
            yield event.plain_result("只有管理员可以使用此命令")
            return
        
        if not target:
            yield event.plain_result("请指定要拉黑的用户：/拉黑 @用户")
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
    
    @filter.command("解禁")
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
            logger.info(f"[LLMTempBan] 管理员 {user_id} 解禁用户 {target_id}")
            yield event.plain_result(f"已解禁用户 {target_id}")
        else:
            yield event.plain_result(f"用户 {target_id} 不在黑名单中")
    
    @filter.command("拉黑列表")
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
            if remaining > 0:
                mins = int(remaining // 60)
                secs = int(remaining % 60)
                lines.append(f"• {uid}: 剩余 {mins}分{secs}秒")
            else:
                lines.append(f"• {uid}: 已过期（即将自动解除）")
                del self.temporary_blacklist[uid]
        
        yield event.plain_result("\n".join(lines))
    
    @filter.command("我的拉黑")
    async def self_ban(self, event: AstrMessageEvent):
        """拉黑自己"""
        user_id = self._normalize_user_id(event.message_obj.sender.user_id)
        duration = self.default_blacklist_duration
        unblock_time = time.time() + duration * 60
        self.temporary_blacklist[user_id] = unblock_time
        yield event.plain_result(
            f"已拉黑自己 {duration} 分钟\n"
            f"到期时间: {time.ctime(unblock_time)}"
        )
    
    @filter.command("解除自拉黑")
    async def self_unban(self, event: AstrMessageEvent):
        """解除自己的拉黑"""
        user_id = self._normalize_user_id(event.message_obj.sender.user_id)
        if user_id in self.temporary_blacklist:
            del self.temporary_blacklist[user_id]
            yield event.plain_result("已解除自拉黑")
        else:
            yield event.plain_result("你没有被拉黑")

    # ==================== LLM 工具 ====================
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
        
        self.ignore_history[session_id].append({
            "time_str": time.strftime("%Y-%m-%d %H:%M"),
            "sender_id": user_id,
            "reason": reason
        })
        
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

    def _extract_target_id(self, target: str) -> str:
        import re
        at_match = re.search(r'CQ:at,qq=(\d+)', target)
        if at_match:
            return at_match.group(1)
        num_match = re.search(r'(\d{5,})', target)
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
                    cleaned.startswith(("http", "file", "base64")) or "/" in cleaned or cleaned.startswith("[")
                ):
                    return cleaned
        return None

    async def terminate(self):
        """插件卸载时保存配置"""
        self.config.save_config()
