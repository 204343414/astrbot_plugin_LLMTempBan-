"""QQ 官方 Bot 静默黑名单与入站洪泛防护。

只保留三件事：
1. 在消息进入 LLM/命令前拦截已拉黑用户；
2. 对真正触发 Bot 的私聊、群聊消息做短窗口洪泛检测，图片按张数计分；
3. 提供唯一 LLM 工具 ban_sender，让模型拉黑当前说话人。

被拉黑后完全静默，不发送嘲讽、不调用 LLM、不操作好友、不查询群角色、不退群。
"""

import hashlib
import json
import os
import re
import time
from collections import deque
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_LLMTempBan"


@register(
    "astrbot_plugin_LLMTempBan",
    "204343414",
    "QQ官方Bot静默拉黑与图片/消息洪泛防护",
    "3.0.0",
    "",
)
class LLMTempBanPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.window_seconds = max(float(config.get("spam_window_seconds", 10)), 1.0)
        self.spam_threshold = max(int(config.get("spam_threshold", 5)), 2)
        self.auto_ban_minutes = max(int(config.get("auto_ban_minutes", 10)), 1)
        self.default_ban_minutes = int(config.get("default_ban_minutes", -1))
        self.max_tracked_senders = max(int(config.get("max_tracked_senders", 2000)), 100)

        self.data_file = self._resolve_data_file()
        self.bans: dict[str, dict] = {}
        self.legacy_global_bans: dict[str, dict] = {}
        self.trackers: dict[str, deque[tuple[float, str, int]]] = {}
        self._load_data()

        logger.info(
            "[LLMTempBan] v3.0 已加载：%d 个会话级拉黑，%d 个旧版全局拉黑；阈值=%d分/%.1f秒",
            len(self.bans), len(self.legacy_global_bans), self.spam_threshold, self.window_seconds,
        )

    def _resolve_data_file(self) -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            root = Path(get_astrbot_data_path())
        except (ImportError, AttributeError, TypeError):
            root = Path("data").resolve()
        directory = root / "plugin_data" / PLUGIN_NAME
        directory.mkdir(parents=True, exist_ok=True)
        return directory / "ban_data.json"

    @staticmethod
    def _scope_key(origin: str, sender_id: str) -> str:
        return f"{origin}|{sender_id}"

    @staticmethod
    def _split_scope_key(scope_key: str) -> tuple[str, str]:
        if "|" not in scope_key:
            return "", scope_key
        return tuple(scope_key.rsplit("|", 1))

    def _load_data(self) -> None:
        if not self.data_file.exists():
            return
        try:
            raw = json.loads(self.data_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("ban_data root must be object")
            if isinstance(raw.get("bans"), dict):
                self.bans = raw["bans"]
                self.legacy_global_bans = raw.get("legacy_global_bans", {}) or {}
            else:
                # 兼容 2.x temporary_blacklist；旧数据没有会话作用域，只能作为全局旧黑名单。
                for sender_id, expires in (raw.get("temporary_blacklist", {}) or {}).items():
                    self.legacy_global_bans[str(sender_id)] = {
                        "expires_at": expires,
                        "created_at": int(time.time()),
                        "source": "legacy_v2",
                        "reason": "从旧版全局黑名单迁移",
                    }
                if self.legacy_global_bans:
                    self._save_data()
            self._cleanup_expired(save=False)
        except Exception as exc:
            logger.error("[LLMTempBan] 持久化数据读取失败；拒绝覆盖原文件: %s", exc)
            self.bans = {}
            self.legacy_global_bans = {}

    def _save_data(self) -> None:
        payload = {
            "version": 3,
            "bans": self.bans,
            "legacy_global_bans": self.legacy_global_bans,
        }
        tmp = self.data_file.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, self.data_file)

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id())
        except Exception:
            return str(event.message_obj.sender.user_id)

    @staticmethod
    def _origin(event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "")

    def _cleanup_expired(self, save: bool = True) -> int:
        now = time.time()
        removed = 0
        for mapping in (self.bans, self.legacy_global_bans):
            for key, entry in list(mapping.items()):
                expires = entry.get("expires_at") if isinstance(entry, dict) else None
                if expires is not None and float(expires) <= now:
                    del mapping[key]
                    removed += 1
        if removed and save:
            self._save_data()
        return removed

    def _matching_ban(self, origin: str, sender_id: str) -> dict | None:
        self._cleanup_expired()
        entry = self.bans.get(self._scope_key(origin, sender_id))
        if isinstance(entry, dict):
            return entry
        legacy = self.legacy_global_bans.get(sender_id)
        return legacy if isinstance(legacy, dict) else None

    def _ban_current_sender(
        self,
        event: AstrMessageEvent,
        duration_minutes: int,
        reason: str,
        source: str,
    ) -> str:
        if event.is_admin():
            return "管理员受保护，未执行拉黑。"
        sender_id = self._sender_id(event)
        origin = self._origin(event)
        if not sender_id or not origin:
            return "无法取得当前说话人的 OpenID 或会话 ID，未执行拉黑。"
        permanent = int(duration_minutes) == -1
        if permanent:
            expires_at = None
            duration_text = "永久"
        else:
            minutes = max(int(duration_minutes or self.default_ban_minutes), 1)
            expires_at = time.time() + minutes * 60
            duration_text = f"{minutes} 分钟"
        self.bans[self._scope_key(origin, sender_id)] = {
            "sender_id": sender_id,
            "origin": origin,
            "expires_at": expires_at,
            "created_at": int(time.time()),
            "source": source,
            "reason": (reason or "未填写")[:300],
        }
        self.trackers.pop(self._scope_key(origin, sender_id), None)
        self._save_data()
        logger.warning(
            "[LLMTempBan] 静默拉黑 sender=%s origin=%s duration=%s source=%s reason=%s",
            sender_id, origin, duration_text, source, (reason or "")[:100],
        )
        return f"已将当前说话人在本会话静默拉黑 {duration_text}。后续消息不会触发 LLM。"

    @staticmethod
    def _targets_bot(event: AstrMessageEvent) -> bool:
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        if "FriendMessage" in origin:
            return True
        if "GroupMessage" not in origin:
            return False
        if bool(getattr(event, "is_at_or_wake_command", False)) or bool(getattr(event, "is_wake", False)):
            return True
        text = str(getattr(event, "message_str", "") or "").lstrip()
        if text.startswith("/"):
            return True
        chain = getattr(event.message_obj, "message", None) or []
        return any(isinstance(component, At) for component in chain)

    @staticmethod
    def _message_fingerprint(event: AstrMessageEvent) -> tuple[str, int]:
        text = re.sub(r"\s+", " ", str(getattr(event, "message_str", "") or "").strip().lower())
        chain = getattr(event.message_obj, "message", None) or []
        image_count = sum(1 for component in chain if isinstance(component, Image))
        # 不下载图片；一条消息里的每张图片直接计分。99 张会在第一条事件立即越过阈值。
        material = f"{text}|images={image_count}"
        fingerprint = hashlib.sha256(material.encode()).hexdigest()[:16]
        return fingerprint, image_count

    def _check_flood(self, event: AstrMessageEvent) -> bool:
        """返回 True 表示本条已触发自动拉黑。"""
        if not self._targets_bot(event):
            return False
        origin = self._origin(event)
        sender_id = self._sender_id(event)
        key = self._scope_key(origin, sender_id)
        now = time.time()
        tracker = self.trackers.setdefault(key, deque())
        while tracker and now - tracker[0][0] > self.window_seconds:
            tracker.popleft()

        fingerprint, image_count = self._message_fingerprint(event)
        duplicate_count = sum(1 for _, previous, _ in tracker if previous == fingerprint)
        # 每条消息基础 1 分；每张图片再加 1 分；重复相同消息额外加 1 分。
        score = 1 + image_count + (1 if duplicate_count else 0)
        tracker.append((now, fingerprint, score))
        total_score = sum(item[2] for item in tracker)

        if len(self.trackers) > self.max_tracked_senders:
            oldest = min(self.trackers, key=lambda item: self.trackers[item][-1][0] if self.trackers[item] else 0)
            if oldest != key:
                self.trackers.pop(oldest, None)

        if total_score < self.spam_threshold:
            return False
        reason = (
            f"自动洪泛检测：{self.window_seconds:g}秒内积分 {total_score} 达到阈值 "
            f"{self.spam_threshold}；本条图片 {image_count} 张；重复指纹 {duplicate_count} 次"
        )
        self._ban_current_sender(
            event,
            self.auto_ban_minutes,
            reason=reason,
            source="auto_flood",
        )
        event.stop_event()
        return True

    @filter.regex(r"[\s\S]*", priority=100)
    async def early_guard(self, event: AstrMessageEvent):
        """最早防线：管理员跳过；黑名单静默停止；洪泛达到阈值立即停止。"""
        if event.is_admin():
            return
        origin = self._origin(event)
        sender_id = self._sender_id(event)
        if self._matching_ban(origin, sender_id):
            event.stop_event()
            logger.info("[LLMTempBan] 静默拦截 sender=%s origin=%s", sender_id, origin)
            return
        self._check_flood(event)

    @filter.on_llm_request()
    async def llm_guard(self, event: AstrMessageEvent, req: ProviderRequest):
        """最终保险：若早期处理顺序变化，LLM 请求前再次检查黑名单。"""
        if event.is_admin():
            return
        if self._matching_ban(self._origin(event), self._sender_id(event)):
            event.stop_event()
            logger.warning("[LLMTempBan] 在 LLM 请求前阻止了黑名单用户")

    @filter.llm_tool(name="ban_sender")
    async def ban_sender(
        self,
        event: AstrMessageEvent,
        duration_minutes: int = -1,
        reason: str = "",
    ):
        """静默拉黑当前正在与 Bot 对话的说话人。仅当对方正在恶俗骚扰、辱骂、诱导敏感内容、开盒式发布真人信息或明显持续攻击时调用；不要因普通意见分歧调用。管理员永远受保护。

        Args:
            duration_minutes(int): 拉黑分钟数；-1 表示永久。默认 -1。
            reason(string): 当前行为的简短事实描述，不要编造身份或历史。
        """
        return self._ban_current_sender(
            event,
            duration_minutes,
            reason=reason or "LLM 判定当前说话人存在明显恶意行为",
            source="llm_ban_sender",
        )

    @filter.command("解禁_")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def unban(self, event: AstrMessageEvent, sender_openid: str = ""):
        """解除指定 OpenID 在所有会话中的拉黑。用法：/解禁_ D138..."""
        sender_openid = str(sender_openid or "").strip()
        if not sender_openid:
            yield event.plain_result("请提供完整 OpenID，例如：/解禁_ D138...")
            return
        removed = 0
        for key in list(self.bans):
            _, sender_id = self._split_scope_key(key)
            if sender_id == sender_openid:
                del self.bans[key]
                removed += 1
        if sender_openid in self.legacy_global_bans:
            del self.legacy_global_bans[sender_openid]
            removed += 1
        self._save_data()
        yield event.plain_result(f"已解除 {sender_openid} 的 {removed} 条拉黑记录。")

    @filter.command("拉黑列表_")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def list_bans(self, event: AstrMessageEvent):
        """查看静默黑名单。"""
        self._cleanup_expired()
        if not self.bans and not self.legacy_global_bans:
            yield event.plain_result("当前静默黑名单为空。")
            return
        now = time.time()
        lines = ["📋 静默黑名单："]
        for entry in list(self.bans.values())[:100]:
            expires = entry.get("expires_at")
            duration = "永久" if expires is None else f"剩余 {max(int(float(expires) - now), 0)} 秒"
            lines.append(
                f"- {entry.get('sender_id')} | {duration} | {entry.get('source')}\n"
                f"  会话: {entry.get('origin')}\n  原因: {entry.get('reason', '')}"
            )
        for sender_id, entry in list(self.legacy_global_bans.items())[:100]:
            expires = entry.get("expires_at")
            duration = "永久" if expires is None else f"剩余 {max(int(float(expires) - now), 0)} 秒"
            lines.append(f"- {sender_id} | {duration} | legacy_global")
        text = "\n".join(lines)
        yield event.plain_result(text[:5000])

    async def terminate(self):
        self._save_data()
        logger.info("[LLMTempBan] 插件已停止")
