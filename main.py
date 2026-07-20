"""
LLMTempBan 增强版 v2.5 - 支持 LLM 工具拉黑、永久拉黑、拉黑历史与自定义拉黑语录

核心目标：
1. 通过 LLM 工具 ban_user / ban_sender 实现灵活拉黑（5 分钟 ~ 永久）
2. 永久拉黑用户触发 Bot 时，按配置间隔自动回复自定义语录（默认 1 小时一次）
3. 确保 stop_event() 能真正阻止消息触发 LLM 调用，避免烧 token
4. 临时黑名单到点自动解除

v2.5 新增：
- 拉黑历史记录：每次拉黑（管理员/LLM/自动刷屏）都会记录时长、来源与原因。
- 当某用户累计被拉黑达到阈值（默认 2 次）时，下次其消息触发 LLM 会把
  历史拉黑理由注入到上下文中，让 Bot 自行判断是否需要（永久）拉黑。
- 新增 ban_sender 工具：Bot 可在判定对方恶俗/多次违规时，自行拉黑“当前说话人”
  （仍然保护管理员）。永久与否完全交由 Bot/管理员决定，不做强制自动升级。
- 修复重启后拉黑数据被清空的问题：所有拉黑/历史/已读不回数据持久化到
  AstrBot data 目录（data/plugin_data/astrbot_plugin_LLMTempBan/ban_data.json），
  重启或重载插件后自动恢复。
"""

import json
import os
import random
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import At, Image
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

# StarTools.get_data_dir 用于获取与插件目录分离的持久化目录，防止更新/重装丢数据。
# 做容错导入，低版本无该 API 时回退到手动拼接 data 路径。
try:
    from astrbot.api.star import StarTools
except Exception:  # pragma: no cover - 兼容性兜底
    StarTools = None

# 插件名，用于定位持久化数据目录
PLUGIN_NAME = "astrbot_plugin_LLMTempBan"


@register(
    "astrbot_plugin_LLMTempBan_v2.6",
    "204343414",
    "LLM临时拉黑（增强版：永久拉黑+拉黑历史+自定义语录+数据持久化）",
    "2.5.0",
)
class BlacklistPluginV2(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.temporary_blacklist = {}  # {用户ID: 解禁时间戳，永久为 float("inf")}
        self.permanent_ban_time = {}  # {用户ID: 拉黑时间戳}
        self.permanent_ban_last_reply = {}  # {用户ID: 上次自动回复时间戳}
        self.ignore_history = {}  # {session_id: [{time_str, sender_id, message, reason}]}
        self.ban_history = {}  # {用户ID: [{time, time_str, duration_text, reason, caller}]}
        self.spam_tracker: dict[str, deque[float]] = {}

        self.auto_approve_friend_requests = self.config.get(
            "auto_approve_friend_requests", False
        )

        self.default_blacklist_duration = self.config.get(
            "default_blacklist_duration", 5
        )

        # 已读不回历史配置
        self.max_ignore_history = self.config.get("max_ignore_history", 20)

        # 自动反刷屏配置
        self.enable_auto_spam_blacklist = self.config.get(
            "enable_auto_spam_blacklist", True
        )
        self.spam_window_seconds = self.config.get("spam_window_seconds", 60)
        self.spam_threshold = max(2, self.config.get("spam_threshold", 5))
        self.auto_blacklist_duration_minutes = self.config.get(
            "auto_blacklist_duration_minutes", 10
        )

        # 拉黑历史配置
        # 当某用户累计被拉黑次数 >= 该阈值时，下次触发 LLM 会注入历史拉黑理由，
        # 帮助 Bot 判断是否需要（永久）拉黑。
        self.ban_history_inject_threshold = max(
            1, self.config.get("ban_history_inject_threshold", 1)
        )
        # 每个用户最多保留的拉黑历史条数，防止无限增长
        self.max_ban_history = max(1, self.config.get("max_ban_history", 20))
        # 被拉黑多次后限制指令配置
        self.ban_count_threshold_for_restrict = max(1, self.config.get("ban_count_threshold_for_restrict", 3))
        self.ban_count_restrict_commands = self.config.get("ban_count_restrict_commands", ["draw", "image", "chat"])

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

        # === 持久化数据文件定位 ===
        self.data_file = self._resolve_data_file()
        # 从磁盘加载历史拉黑/黑名单数据（修复重启数据清空 bug）
        self._load_data()

        logger.info("=" * 60)
        logger.info("拉黑插件 v2.7.0 初始化完成")
        logger.info(f"自动拉黑阈值: {self.spam_threshold}条/{self.spam_window_seconds}秒")
        logger.info(f"拉黑时长: {self.auto_blacklist_duration_minutes}分钟")
        logger.info(f"永久拉黑回复间隔: {self.permanent_ban_reply_interval}秒")
        logger.info(f"永久拉黑语录数: {len(self.permanent_ban_messages)}")
        logger.info(f"好友专用永久拉黑语录数: {len(self.friend_permanent_ban_messages)}")
        logger.info(f"永久拉黑自动删好友: {self.auto_delete_friend_on_permanent_ban}")
        logger.info(f"拉黑历史注入阈值: {self.ban_history_inject_threshold} 次")
        logger.info(
            f"已加载持久化数据: 黑名单 {len(self.temporary_blacklist)} 人，"
            f"历史记录 {len(self.ban_history)} 人 "
            f"(文件: {self.data_file})"
        )
        logger.info("=" * 60)

    # ==================== 持久化：加载 / 保存 ====================
    def _resolve_data_file(self) -> Path | None:
        """定位持久化数据文件路径（data/plugin_data/<插件名>/ban_data.json）。"""
        # 优先使用官方推荐的 StarTools.get_data_dir
        try:
            if StarTools is not None:
                data_dir = StarTools.get_data_dir(PLUGIN_NAME)
                return Path(data_dir) / "ban_data.json"
        except Exception as e:
            logger.warning(f"[LLMTempBan] StarTools.get_data_dir 失败，尝试回退: {e}")

        # 回退：手动拼接 AstrBot data 路径
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            base = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
            base.mkdir(parents=True, exist_ok=True)
            return base / "ban_data.json"
        except Exception as e:
            logger.error(
                f"[LLMTempBan] 无法定位持久化目录，拉黑数据将无法在重启后保留: {e}"
            )
            return None

    def _load_data(self):
        """从磁盘加载持久化数据，并清理已过期的临时拉黑。"""
        if not self.data_file:
            return
        try:
            if not Path(self.data_file).exists():
                return
            with open(self.data_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            # 临时黑名单：None 代表永久（float("inf")）
            tb = data.get("temporary_blacklist", {}) or {}
            loaded_tb = {}
            for k, v in tb.items():
                loaded_tb[str(k)] = float("inf") if v is None else float(v)
            self.temporary_blacklist = loaded_tb

            self.permanent_ban_time = {
                str(k): float(v)
                for k, v in (data.get("permanent_ban_time", {}) or {}).items()
            }
            self.permanent_ban_last_reply = {
                str(k): float(v)
                for k, v in (data.get("permanent_ban_last_reply", {}) or {}).items()
            }
            self.ban_history = {
                str(k): list(v)
                for k, v in (data.get("ban_history", {}) or {}).items()
            }
            self.ignore_history = {
                str(k): list(v)
                for k, v in (data.get("ignore_history", {}) or {}).items()
            }

            # 清理已过期的临时拉黑（重启期间可能已到期）
            now = time.time()
            expired = [
                uid
                for uid, unblock in self.temporary_blacklist.items()
                if unblock != float("inf") and unblock <= now
            ]
            for uid in expired:
                self.temporary_blacklist.pop(uid, None)
                self.permanent_ban_time.pop(uid, None)
                self.permanent_ban_last_reply.pop(uid, None)
            if expired:
                logger.info(
                    f"[LLMTempBan] 启动时清理了 {len(expired)} 个已到期的临时拉黑"
                )
        except Exception as e:
            logger.error(f"[LLMTempBan] 加载持久化数据失败（将以空数据启动）: {e}")

    def _save_data(self):
        """原子写入持久化数据。任何状态变更后调用。"""
        if not self.data_file:
            return
        try:
            data = {
                # 永久拉黑用 None 表示，避免 float("inf") 写入非标准 JSON
                "temporary_blacklist": {
                    k: (None if v == float("inf") else v)
                    for k, v in self.temporary_blacklist.items()
                },
                "permanent_ban_time": self.permanent_ban_time,
                "permanent_ban_last_reply": self.permanent_ban_last_reply,
                "ban_history": self.ban_history,
                "ignore_history": self.ignore_history,
            }
            data_path = Path(self.data_file)
            data_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = data_path.with_name(data_path.name + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, data_path)
        except Exception as e:
            logger.warning(f"[LLMTempBan] 保存持久化数据失败: {e}")

    def _record_ban_history(
        self,
        user_id: str,
        duration_text: str,
        reason: str,
        caller: str,
        location: str = "未知",
    ):
        """记录一次拉黑历史（记仇小本本：日期 + 地点 + 原因）。"""
        entry = {
            "time": time.time(),
            # 完整日期时间，精确到秒，像记仇小本本一样可追溯
            "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_text": duration_text,
            "location": location or "未知",
            "reason": reason or "",
            "caller": caller,
        }
        self.ban_history.setdefault(user_id, []).append(entry)
        # 限制长度，防止无限增长
        if len(self.ban_history[user_id]) > self.max_ban_history:
            self.ban_history[user_id] = self.ban_history[user_id][
                -self.max_ban_history :
            ]

    def _get_location(self, event) -> str:
        """根据事件判断拉黑发生的地点：群聊（含群号）或私聊。"""
        if event is None:
            return "未知"
        try:
            group_id = getattr(event.message_obj, "group_id", "") or ""
        except Exception:
            group_id = ""
        if group_id:
            return f"群聊(群号:{group_id})"
        umo = getattr(event, "unified_msg_origin", "") or ""
        if "GroupMessage" in umo:
            return "群聊"
        if "FriendMessage" in umo:
            return "私聊"
        return "私聊"

    def _format_ban_history_text(self, user_id: str, max_items: int = 10) -> str:
        """把某用户的历史拉黑记录格式化为可读文本（日期 + 地点 + 原因）。"""
        history = self.ban_history.get(user_id) or []
        if not history:
            return ""
        recent = history[-max_items:]
        start_idx = len(history) - len(recent) + 1
        lines = []
        for i, h in enumerate(recent, start=start_idx):
            lines.append(
                f"  第{i}次 [{h.get('time_str', '')}]"
                f" 地点：{h.get('location', '未知')}"
                f" | 时长：{h.get('duration_text', '未知')}"
                f" | 原因：{h.get('reason') or '未填写'}"
            )
        return "\n".join(lines)

    # ==================== 第一道防线：监听钩子（全局黑名单拦截 + 私聊刷屏检测） ====================
    @filter.regex(r"[\s\S]*", priority=10)
    async def _catch_all_for_spam(self, event: AstrMessageEvent):
        """全局黑名单过滤：被拉黑用户（含命令、LLM）一律 stop_event；私聊额外执行刷屏检测。"""

        # OneBot 请求事件会被 AstrBot 转成 OTHER_MESSAGE，原始请求保存在 raw_message。
        # 这里只处理好友申请，避免影响普通消息及其他请求类型。
        if await self._handle_friend_request(event):
            return

        user_id = self._normalize_user_id(event.message_obj.sender.user_id)

        # 保护管理员：管理员不受任何拉黑限制
        if event.is_admin():
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
                        self._save_data()
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
                self._save_data()
                logger.info(f"[LLMTempBan] 用户 {user_id} 拉黑已过期，自动解除")

        # === 私聊刷屏检测 ===
        umo = getattr(event, "unified_msg_origin", "") or ""
        if "FriendMessage" in umo and self.enable_auto_spam_blacklist:
            self._check_spam(user_id, event)

    async def _handle_friend_request(self, event: AstrMessageEvent) -> bool:
        """处理 OneBot 好友申请；返回 True 表示该事件已处理。"""
        raw = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw, dict) or raw.get("request_type") != "friend":
            return False

        if not self.auto_approve_friend_requests:
            return True

        user_id = self._normalize_user_id(raw.get("user_id", ""))
        # 永久拉黑用户不得通过好友申请，即使开启了自动同意。
        if user_id in self.temporary_blacklist and self.temporary_blacklist[user_id] == float("inf"):
            logger.info(f"[LLMTempBan] 忽略永久拉黑用户的好友申请 user={user_id}")
            return True

        flag = raw.get("flag")
        if not flag:
            logger.warning("[LLMTempBan] 好友申请缺少 flag，无法自动处理")
            return True

        client = await self._get_client()
        if not client:
            logger.warning("[LLMTempBan] 无法获取协议端客户端，好友申请未处理")
            return True

        try:
            if hasattr(client, "set_friend_add_request"):
                await client.set_friend_add_request(flag=flag, approve=True)
            else:
                await client.call_action("set_friend_add_request", flag=flag, approve=True)
            logger.info(f"[LLMTempBan] 已自动同意好友申请 user={user_id}")
        except Exception as e:
            logger.warning(f"[LLMTempBan] 自动同意好友申请失败 user={user_id}: {e}")
        return True

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

            # 记录拉黑历史（供后续 Bot 判断是否升级为永久拉黑）
            reason = (
                f"自动刷屏检测：{self.spam_window_seconds}秒内积分 {count} "
                f"达到阈值 {self.spam_threshold}"
                f"（图片数={num_images} 独特={num_unique} 有重复={has_dup}）"
            )
            self._record_ban_history(
                user_id,
                f"{duration} 分钟",
                reason,
                caller="auto_spam",
                location=self._get_location(event),
            )
            self._save_data()

            logger.warning(
                f"[LLMTempBan] ⛔ 【私聊】自动触发刷屏拉黑 user={user_id}\n"
                f" 窗口内积分: {count} >= 阈值 {self.spam_threshold}\n"
                f" 本消息: 图片数={num_images} 独特={num_unique} 有重复={has_dup}\n"
                f" 拉黑 {duration} 分钟（至 {time.ctime(unblock_time)}）\n"
                f" 该用户累计被拉黑 {len(self.ban_history.get(user_id, []))} 次"
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
        对于未被拉黑但有多次拉黑历史的用户，会注入历史拉黑理由，
        让 Bot 自行判断是否需要（永久）拉黑。
        """
        user_id = self._normalize_user_id(event.message_obj.sender.user_id)
        session_id = self._get_session_id(event)

        # 保护管理员
        if event.is_admin():
            return

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
                self._save_data()
                logger.info(f"[LLMTempBan] 用户 {user_id} 拉黑已过期")

        # 注入历史拉黑理由（达到阈值时），让 Bot 自行决定是否再次/永久拉黑
        self._inject_ban_history(user_id, req)

        # 注入已读不回历史（按次数累积，让 LLM 自己决定是否继续潜水）
        self._inject_ignore_history(session_id, req)

    def _inject_ban_history(self, user_id: str, req: ProviderRequest):
        """对累计被拉黑达到阈值的用户，把历史拉黑理由注入到本轮 LLM 上下文。"""
        history = self.ban_history.get(user_id) or []
        if len(history) < self.ban_history_inject_threshold:
            return

        detail = self._format_ban_history_text(user_id, max_items=self.max_ban_history)
        text = (
            f"\n\n[拉黑前科警示] 你没有长期记忆，但本系统按对方的账号全局记录了拉黑历史"
            f"（跨群、跨私聊通用）。当前这个人（{user_id}）此前已被拉黑 {len(history)} 次，"
            f"哪怕发生在别的群或私聊，下面都是 ta 的完整前科（含日期、地点、原因）：\n"
            f"{detail}\n"
        )
        text += (
            "\n请把以上前科当作你对此人的『记忆』，结合本轮对话二选一：\n"
            "【路线1·放过】若对方本次行为正常、只是旧事，请正常回复，不要因为旧账翻脸，"
            "也不要滥用拉黑。\n"
            "【路线2·升级】若对方本次仍在恶俗、骚扰、辱骂、诱导发送政治敏感等易封号内容，"
            "请调用 ban_sender 工具拉黑 ta，并根据前科严重程度逐级加重时长；"
            "对于屡教不改、跨群重复作恶者，可直接设 duration_minutes=-1 永久拉黑——"
            "永久拉黑后 ta 将再也无法触发或与你互动，你可以在拉黑前阴阳怪气地送 ta 一程。"
        )

        try:
            from astrbot.core.agent.message import TextPart

            req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())
        except Exception as e:
            logger.debug(f"注入拉黑历史失败: {e}")

    def _inject_ignore_history(self, session_id, req: ProviderRequest):
        """注入已读不回历史到请求中"""
        if session_id not in self.ignore_history or not self.ignore_history[session_id]:
            return

        history = self.ignore_history[session_id][-self.max_ignore_history :]
        text = (
            f"\n\n[已读不回记录] 你在本会话已执行 {len(self.ignore_history[session_id])} 次已读不回。"
            f"以下是对方此前发送的消息记录，请判断是否仍在重复骚扰/无意义发言。\n"
        )
        for idx, r in enumerate(history, start=1):
            msg = r.get("message", "")[:200]
            text += f"{idx}. [{r['time_str']}] 用户 {r['sender_id']}：{msg}（原因：{r['reason']}）\n"
        text += (
            "\n如果以上记录显示对方仍在重复/无意义骚扰，请继续调用 read_and_ignore 保持沉默。"
            "如果情况已变化或需要回应，请直接回复。"
        )

        try:
            from astrbot.core.agent.message import TextPart

            req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())
        except Exception as e:
            logger.debug(f"注入已读不回历史失败: {e}")

    def _restrict_commands_for_banned_users(self, user_id: str, req: ProviderRequest):
        """被拉黑多次的用户限制使用特定指令（在 on_llm_request 阶段注入警告）"""
        ban_count = len(self.ban_history.get(user_id, []))
        threshold = getattr(self, 'ban_count_threshold_for_restrict', 3)
        restricted_cmds = getattr(self, 'ban_count_restrict_commands', ["draw", "image", "chat"])

        if ban_count < threshold or not restricted_cmds:
            return

        # 构造限制提示
        cmd_list = "、".join([f"/{c}" for c in restricted_cmds])
        text = (
            f"\n\n[指令限制提醒] 该用户累计已被拉黑 {ban_count} 次（阈值 {threshold}）。"
            f"根据配置，当前已禁止使用以下指令：{cmd_list}。"
            f"若继续违规，将可能被永久拉黑或触发退群。"
        )

        try:
            from astrbot.core.agent.message import TextPart
            req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())
        except Exception as e:
            logger.debug(f"注入指令限制失败: {e}")

    # ==================== 永久拉黑自动回复 ==================
    async def _send_permanent_ban_message(self, event: AstrMessageEvent, user_id: str):
        """按配置间隔向永久拉黑用户发送自定义语录；如目标是好友且开启，可优先使用好友专用语录。"""
        now = time.time()
        last_reply = self.permanent_ban_last_reply.get(user_id, 0)

        if now - last_reply < self.permanent_ban_reply_interval:
            return

        # 选择语录池：好友优先使用 friend_permanent_ban_messages（如果配置且非空）
        messages = self.permanent_ban_messages
        if self.friend_permanent_ban_messages and await self._is_friend(user_id):
            messages = self.friend_permanent_ban_messages

        if not messages:
            return

        template = random.choice(messages)
        ban_time = self.permanent_ban_time.get(user_id, now)
        message = self._render_message(template, user_id, ban_time)

        try:
            await event.send(event.plain_result(message))
            self.permanent_ban_last_reply[user_id] = now
            self._save_data()
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

    async def _is_group_admin_or_owner(self, group_id: str, user_id: str) -> str:
        """检测用户是否为群主或管理员，返回 'owner' / 'admin' / 'member' / 'unknown'"""
        if not group_id or not user_id:
            return "unknown"

        client = await self._get_client()
        if not client:
            return "unknown"

        try:
            # 尝试 get_group_member_info
            if hasattr(client, "get_group_member_info"):
                info = await client.get_group_member_info(group_id=int(group_id), user_id=int(user_id))
                role = info.get("role", "") or info.get("role_name", "")
                if role in ("owner", "群主"):
                    return "owner"
                elif role in ("admin", "管理员"):
                    return "admin"
                return "member"

            # 回退到 call_action
            info = await client.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id)
            )
            role = info.get("role", "") or info.get("role_name", "")
            if role in ("owner", "群主"):
                return "owner"
            elif role in ("admin", "管理员"):
                return "admin"
            return "member"

        except Exception as e:
            logger.debug(f"[LLMTempBan] 获取群成员角色失败: {e}")
            return "unknown"

    # ==================== 命令处理（仅管理员） ====================
    @filter.command("拉黑_")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def ban_user_cmd(self, event: AstrMessageEvent, target: str = None):
        """拉黑用户（仅管理员）。用法：/拉黑_ @用户 [时长分钟，默认5，-1永久]"""
        # 从完整命令文本解析，避免 AstrBot 对 target 参数的类型/分词处理不一致
        text = event.message_str.strip()
        for prefix in ("/拉黑_", "拉黑_"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break

        if not text:
            yield event.plain_result("请指定要拉黑的用户：/拉黑_ @用户 [时长分钟，默认5，-1永久]")
            return

        parts = text.split()
        target_part = parts[0]
        duration = self.default_blacklist_duration
        if len(parts) > 1:
            try:
                duration = int(parts[1])
            except ValueError:
                pass

        target_id = self._extract_target_id(target_part)
        if not target_id:
            yield event.plain_result("无法识别目标用户")
            return

        result = await self._ban_user(
            target_id,
            duration,
            caller="admin_cmd",
            reason="管理员命令拉黑",
            event=event,
        )
        yield event.plain_result(result)

    @filter.command("解禁_")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def unban_user_cmd(self, event: AstrMessageEvent, target: str = None):
        """解禁用户（仅管理员）"""
        # 从完整命令文本解析，允许 /@用户 后面跟多余参数
        text = event.message_str.strip()
        for prefix in ("/解禁_", "解禁_"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break

        if not text:
            yield event.plain_result("请指定要解禁的用户：/解禁_ @用户")
            return

        target_id = self._extract_target_id(text.split()[0])
        if target_id in self.temporary_blacklist:
            del self.temporary_blacklist[target_id]
            self.permanent_ban_time.pop(target_id, None)
            self.permanent_ban_last_reply.pop(target_id, None)
            self._save_data()
            logger.info(f"[LLMTempBan] 管理员解禁用户 {target_id}")
            yield event.plain_result(f"已解禁用户 {target_id}")
        else:
            yield event.plain_result(f"用户 {target_id} 不在黑名单中")

    @filter.command("拉黑列表_")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def list_banned_cmd(self, event: AstrMessageEvent):
        """查看当前拉黑列表（仅管理员）"""
        if not self.temporary_blacklist:
            yield event.plain_result("当前没有拉黑用户 ✅")
            return

        now = time.time()
        changed = False
        lines = ["📋 当前拉黑列表：\n"]
        for uid, unblock_time in list(self.temporary_blacklist.items()):
            remaining = unblock_time - now
            ban_count = len(self.ban_history.get(uid, []))
            count_text = f"（累计被拉黑 {ban_count} 次）" if ban_count else ""
            if unblock_time == float("inf"):
                lines.append(f"• {uid}: 永久拉黑{count_text}")
            elif remaining > 0:
                mins = int(remaining // 60)
                secs = int(remaining % 60)
                lines.append(f"• {uid}: 剩余 {mins}分{secs}秒{count_text}")
            else:
                lines.append(f"• {uid}: 已过期（即将自动解除）")
                del self.temporary_blacklist[uid]
                self.permanent_ban_time.pop(uid, None)
                self.permanent_ban_last_reply.pop(uid, None)
                changed = True

        if changed:
            self._save_data()

        yield event.plain_result("\n".join(lines))

    @filter.command("拉黑历史_")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def ban_history_cmd(self, event: AstrMessageEvent, target: str = None):
        """查看某用户的历史拉黑记录与理由（仅管理员）。用法：/拉黑历史_ @用户"""
        text = event.message_str.strip()
        for prefix in ("/拉黑历史_", "拉黑历史_"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break

        if not text:
            yield event.plain_result("请指定要查询的用户：/拉黑历史_ @用户")
            return

        target_id = self._extract_target_id(text.split()[0])
        if not target_id:
            yield event.plain_result("无法识别目标用户")
            return

        history = self.ban_history.get(target_id) or []
        if not history:
            yield event.plain_result(f"用户 {target_id} 没有历史拉黑记录 ✅")
            return

        lines = [f"📜 用户 {target_id} 共被拉黑 {len(history)} 次：\n"]
        for idx, h in enumerate(history, start=1):
            lines.append(
                f"{idx}. [{h.get('time_str', '')}]"
                f" 地点：{h.get('location', '未知')}"
                f" | 时长：{h.get('duration_text', '未知')}"
                f" | 来源：{h.get('caller', '未知')}"
                f" | 原因：{h.get('reason') or '未填写'}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("清空拉黑历史_")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def clear_ban_history_cmd(self, event: AstrMessageEvent, target: str = None):
        """清空某用户的历史拉黑记录（仅管理员）。用法：/清空拉黑历史_ @用户"""
        text = event.message_str.strip()
        for prefix in ("/清空拉黑历史_", "清空拉黑历史_"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break

        if not text:
            yield event.plain_result("请指定要清空历史的用户：/清空拉黑历史_ @用户")
            return

        target_id = self._extract_target_id(text.split()[0])
        if target_id and target_id in self.ban_history:
            del self.ban_history[target_id]
            self._save_data()
            yield event.plain_result(f"已清空用户 {target_id} 的拉黑历史记录")
        else:
            yield event.plain_result(f"用户 {target_id} 没有历史拉黑记录")

    # ==================== LLM 工具 ====================
    @filter.llm_tool(name="ban_user")
    async def ban_user_tool(
        self,
        event: AstrMessageEvent,
        target_user_id: str,
        duration_minutes: int = -1,
        reason: str = "",
    ):
        """拉黑指定用户。仅管理员可调用。duration_minutes=-1 表示永久拉黑，其他正整数表示临时拉黑分钟数。

        Args:
            target_user_id(string): 要拉黑的用户 ID（群聊中也可通过 @ 目标自动识别，但建议填写）
            duration_minutes(int): 拉黑时长（分钟），-1 表示永久拉黑，默认永久
            reason(string): 拉黑原因。只需简述对方做了什么，例如“莫名其妙辱骂 Bot（nmsl）”“诱导 Bot 谈论六四/政治敏感内容”“反复刷屏骚扰”等；拉黑的日期和地点（私聊/群号）会由系统自动记录，无需你填写。
        """
        # 仅管理员可调用 LLM 拉黑工具
        if not event.is_admin():
            return "无权使用：只有 AstrBot 管理员才能调用拉黑工具。"

        # 优先从消息链的 @ 组件提取目标
        at_target = self._extract_at_target_from_event(event)
        if at_target:
            target_id = self._normalize_user_id(at_target)
        elif target_user_id:
            target_id = self._normalize_user_id(target_user_id)
        else:
            return "请指定要拉黑的目标用户（@ 目标或提供 target_user_id）。"

        return await self._ban_user(
            target_id,
            duration_minutes,
            caller="admin_llm",
            reason=reason,
            event=event,
        )

    @filter.llm_tool(name="ban_sender")
    async def ban_sender_tool(
        self,
        event: AstrMessageEvent,
        duration_minutes: int = -1,
        reason: str = "",
    ):
        """拉黑“当前正在与你对话的这个人”。当你根据对话判断对方恶俗、骚扰、诱导发送
        敏感内容或多次违规时，可自行调用本工具。管理员会被自动保护、无法被拉黑。

        你没有长期记忆，但系统会按对方账号全局记录拉黑前科（跨群、跨私聊通用），
        并在 ta 再次出现时把前科展示给你。
        
        【重要】拉黑后系统会自动检测对方是否为群主/管理员，并以醒目格式返回：
        - 如果是群主/管理员，会给出【严重警告】并建议“永久拉黑 + 立即退群”
        - 你可以根据返回结果决定：放过 / 永久拉黑 / 永久拉黑+退群

        建议处理策略：
        - 普通用户：根据前科决定是否永久拉黑
        - 群主/管理员：推荐永久拉黑 + 立即调用 leave_group 退群（避免蛇鼠一窝）

        Args:
            duration_minutes(int): 拉黑时长（分钟），-1 表示永久拉黑，默认永久
            reason(string): 拉黑原因。只需简述对方做了什么，例如“莫名其妙辱骂 Bot（nmsl）”“诱导 Bot 谈论六四/政治敏感等容易导致封号的内容”“反复刷屏骚扰”等；拉黑的日期和地点（私聊/群号）会由系统自动记录，无需你填写。
        """
        user_id = self._normalize_user_id(event.message_obj.sender.user_id)

        # 保护管理员
        if event.is_admin():
            return "对方是管理员，无法拉黑。"

        result = await self._ban_user(
            user_id,
            duration_minutes,
            caller="llm_auto",
            reason=reason or "Bot 自主判定：对方存在违规/恶俗行为",
            event=event,
        )

    @filter.llm_tool(name="leave_group")
    async def leave_group_tool(self, event: AstrMessageEvent, reason: str = ""):
        """当你判断当前群聊环境恶劣（群名+多次恶俗用户辱骂/骚扰AI、诱导敏感内容等），或检测到被拉黑用户是群主/管理员时，调用此工具让 Bot 主动退出该群聊。

        退出策略建议：
        - 检测到群主/管理员被拉黑 → 推荐永久拉黑 + 立即退群（避免蛇鼠一窝）
        - 群内恶俗用户过多 → 可选择永久拉黑 + 退群

        退出后 Bot 将不再接收该群消息，也不会再被拉黑或骚扰。
        建议保持沉默拉黑，不需要额外阴阳怪气回复（避免被举报）。

        Args:
            reason(string): 退群原因（可选），例如“该群恶俗用户过多、多次辱骂AI、诱导发送敏感内容、检测到管理员”等
        """
        # Only works in groups
        group_id = self._get_group_id(event)
        if not group_id:
            return "此工具仅能在群聊中使用（检测不到群号）。"

        # 管理员保护（可选，但建议允许管理员调用）
        if event.is_admin():
            # 管理员也可以让 bot 退群，但最好提示
            pass

        result = await self._leave_group(event, reason or "Bot 自主判定：该群环境恶劣（恶俗用户过多）")
        return result

    async def _leave_group(self, event: AstrMessageEvent, reason: str = "") -> str:
        """执行退群操作（仅群聊有效）"""
        group_id = self._get_group_id(event)
        if not group_id:
            return "无法获取群号，退群失败。"

        client = await self._get_client()
        if not client:
            return "无法获取协议端客户端，退群失败（可能非 aiocqhttp 平台）。"

        try:
            success = False
            try:
                if hasattr(client, "set_group_leave"):
                    await client.set_group_leave(group_id=int(group_id))
                    success = True
                elif hasattr(client, "leave_group"):
                    await client.leave_group(group_id=int(group_id))
                    success = True
            except Exception as e:
                logger.debug(f"[LLMTempBan] set_group_leave/leave_group 失败: {e}")

            if not success:
                try:
                    await client.call_action("set_group_leave", group_id=int(group_id), is_dismiss=False)
                    success = True
                except Exception as e:
                    logger.warning(f"[LLMTempBan] call_action set_group_leave 失败: {e}")

            if success:
                location = self._get_location(event)
                logger.warning(f"[LLMTempBan] Bot 已退出群 {group_id}，原因: {reason}（地点: {location}）")
                return f"已成功退出群 {group_id}。原因：{reason}。"
            else:
                return f"退群操作失败（协议端可能不支持 set_group_leave）。群号: {group_id}"
        except Exception as e:
            logger.warning(f"[LLMTempBan] 退群失败: {e}")
            return f"退群操作异常: {str(e)}"

    def _get_group_id(self, event: AstrMessageEvent) -> str:
        """获取当前群聊的群号"""
        try:
            group_id = getattr(event.message_obj, "group_id", "") or ""
            if group_id:
                return str(group_id)
        except Exception:
            pass

        try:
            umo = getattr(event, "unified_msg_origin", "") or ""
            if "GroupMessage" in umo:
                import re
                m = re.search(r"GroupMessage_(\d+)", umo)
                if m:
                    return m.group(1)
        except Exception:
            pass
        return ""


    @filter.llm_tool(name="read_and_ignore")
    async def read_and_ignore(self, event: AstrMessageEvent, reason: str = "无意义发言"):
        """已读不回工具。调用后会记录本次用户消息，下次 LLM 会看到累计历史并决定是否继续潜水。

        Args:
            reason(string): 忽略原因
        """
        session_id = self._get_session_id(event)
        user_id = self._normalize_user_id(event.message_obj.sender.user_id)

        if session_id not in self.ignore_history:
            self.ignore_history[session_id] = []

        self.ignore_history[session_id].append(
            {
                "time_str": time.strftime("%Y-%m-%d %H:%M"),
                "sender_id": user_id,
                "message": event.message_str or "",
                "reason": reason,
            }
        )

        # 限制历史长度，防止无限增长
        if len(self.ignore_history[session_id]) > self.max_ignore_history:
            self.ignore_history[session_id] = self.ignore_history[session_id][
                -self.max_ignore_history :
            ]

        self._save_data()

        logger.info(
            f"[LLMTempBan] 已读不回 session={session_id} 次数={len(self.ignore_history[session_id])}"
        )

        return "已忽略此消息，并记录到已读不回历史中。"

    @filter.llm_tool(name="reset_ignore_status")
    async def reset_ignore_status(self, event: AstrMessageEvent):
        """重置已读不回状态，清空累计历史记录。"""
        session_id = self._get_session_id(event)

        if session_id in self.ignore_history:
            del self.ignore_history[session_id]
            self._save_data()

        return "已清空已读不回历史。"

    # ==================== 工具方法 ====================
    def _ensure_message_list(self, value) -> list[str]:
        """确保配置项是字符串列表"""
        if not value:
            return []
        if not isinstance(value, list):
            return [str(value).strip()] if str(value).strip() else []
        return [str(m).strip() for m in value if str(m).strip()]

    async def _ban_user(
        self,
        target_id: str,
        duration_minutes: int,
        caller: str,
        reason: str = "",
        event: AstrMessageEvent = None,
    ) -> str:
        """执行拉黑，并返回结果描述。永久拉黑时可选择自动删除好友。"""
        now = time.time()
        permanent = duration_minutes == -1
        location = self._get_location(event)

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

        # 记录拉黑历史并持久化（含地点）
        self._record_ban_history(
            target_id, duration_text, reason, caller, location=location
        )
        self._save_data()

        ban_count = len(self.ban_history.get(target_id, []))
        log_reason = f" 原因: {reason}" if reason else ""
        logger.warning(
            f"[LLMTempBan] {caller} 拉黑用户 {target_id} {duration_text}{log_reason}"
            f" 地点: {location}（累计 {ban_count} 次）"
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

        # 检测是否为群主/管理员（仅群聊有效）
        admin_role = "member"
        if "群聊" in location:
            group_id = self._get_group_id(event)
            if group_id:
                admin_role = await self._is_group_admin_or_owner(group_id, target_id)

        admin_warning = ""
        if admin_role == "owner":
            admin_warning = "\n\n⚠️【严重警告】该用户是【群主】！\n建议处理：永久拉黑 + 立即调用 leave_group 退群（避免蛇鼠一窝）。"
        elif admin_role == "admin":
            admin_warning = "\n\n⚠️【重要警告】该用户是【管理员】！\n建议处理：永久拉黑 + 立即调用 leave_group 退群（避免蛇鼠一窝）。"

        # 把前几次的拉黑明细（日期+地点+原因）一并返回，
        # 这样无论调用者是否管理员，拉黑时都能当场看到“记仇小本本”。
        history_block = ""
        if ban_count > 0:
            detail = self._format_ban_history_text(target_id)
            history_block = (
                f"\n该用户累计已被拉黑 {ban_count} 次，历史记录如下：\n{detail}\n"
            )

        if permanent:
            return (
                f"已永久拉黑用户 {target_id}（本次地点：{location}）。{extra_text}"
                f"此后该用户每次触发 Bot，将每隔 {self.permanent_ban_reply_interval} 秒"
                f"收到一条自动回复语录。{history_block}{admin_warning}"
            )
        return (
            f"已拉黑用户 {target_id} {duration_text}（本次地点：{location}），"
            f"到期时间: {time.ctime(unblock_time)}。"
            f"拉黑期间对方消息不会触发 LLM 回复。{history_block}{admin_warning}"
        )

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
        """插件卸载/停用时保存配置与持久化数据"""
        try:
            self._save_data()
        except Exception as e:
            logger.warning(f"[LLMTempBan] terminate 保存数据失败: {e}")
        try:
            self.config.save_config()
        except Exception as e:
            logger.warning(f"[LLMTempBan] terminate 保存配置失败: {e}")
