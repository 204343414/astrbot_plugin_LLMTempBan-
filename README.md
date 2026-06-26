# astrbot_plugin_LLMTempBan（增强版 v2.6.0）

## 🆕 v2.6.0 更新

- **拉黑理由升级为「记仇小本本」格式**：每条拉黑历史现包含
  - 🗓️ **日期**：精确到秒的完整日期时间（`YYYY-MM-DD HH:MM:SS`），由系统自动记录；
  - 📍 **地点**：自动识别是 **私聊** 还是 **群聊（含群号）**，无需 LLM 填写，杜绝编造；
  - 📝 **原因**：由 LLM/管理员填写，工具描述里已给出范例（如“莫名其妙辱骂 Bot（nmsl）”“诱导谈六四/政治敏感等易封号内容”“反复刷屏”等）。
- **拉黑时当场可见前几次理由**：`ban_user` / `ban_sender` 的返回值现会附带该用户**完整历史拉黑明细**（日期+地点+原因）。这样即使调用者是管理员（注入式上下文对管理员默认跳过），也能在拉黑那一刻看到“账本”。
- `/拉黑历史_ @用户` 输出同步显示地点字段；旧版本数据无地点字段时显示「未知」，完全向后兼容。


一个为 AstrBot 设计的 LLM 临时拉黑屏蔽插件，核心目的是 **防刷屏、防恶俗、防止用户诱导 Bot 发送敏感内容**。支持 AstrBot 自带的管理员权限体系，提供管理员拉黑、已读不回、自动刷屏/图片 spam 检测、永久拉黑自定义语录、QQ 好友检测与自动删除好友等功能。

## 🆕 v2.5.0 更新

- **拉黑历史记录**：每次拉黑（管理员命令 / LLM 工具 / 自动刷屏）都会记录时长、来源与原因。
- **多次拉黑警示注入**：当某用户累计被拉黑达到阈值（默认 **2 次**，可在配置 `ban_history_inject_threshold` 调整）后，其下次触发 LLM 时，会把历史拉黑理由注入到本轮上下文，让 Bot **看到前面几次的拉黑理由**并自行判断是否需要再次甚至永久拉黑。
- **Bot 自主拉黑工具 `ban_sender`**：Bot 可在判定对方恶俗 / 多次违规时，**自行决定**拉黑“当前说话人”，时长由 Bot 决定（`-1` 永久）。管理员被自动保护，无法被拉黑。
  > 注意：本插件不做“到 N 次就强制永久”的硬性自动升级，永久与否完全交给 Bot/管理员判断，避免误伤。
- **修复重启数据清空 bug**：所有拉黑 / 历史 / 已读不回数据现持久化到 AstrBot data 目录（`data/plugin_data/astrbot_plugin_LLMTempBan/ban_data.json`），**重启或重载插件后自动恢复**，启动时会自动清理已到期的临时拉黑。
- 新增命令：`/拉黑历史_ @用户`、`/清空拉黑历史_ @用户`。

> 参考了 [astrbot_plugin_recall_cancel](https://github.com/muyouzhi6/astrbot_plugin_recall_cancel) 的代码风格和钩子使用（on_llm_request 尽早拦截 + stop_event() + 日志），并查阅 AstrBot wiki 和官方示例确认兼容性。所有钩子使用合理优先级，不会干扰其他插件。

## ✨ 功能概览

### 🚫 防刷屏自动拉黑

- 检测到同一用户在 **x秒内连续 y次主动向 Bot 发消息**（私聊），自动触发拉黑 **z分钟**。
- 触发后 **立即 `event.stop_event()` 撤销 LLM 调用**，避免烧 token。
- 持续发消息会延长拉黑时间（惩罚机制）。
- 同一图片重复发送会额外加速触发。
- 仅对非管理员生效。

### 🛠️ 管理员拉黑工具 / 命令

- **LLM 工具 `ban_user`**：管理员可直接让 LLM 调用，拉黑指定用户，支持 `duration_minutes=-1` 永久拉黑。
- **命令 `/拉黑_ @用户 [时长]`**：管理员手动拉黑，时长默认 5 分钟，`-1` 表示永久。
- **命令 `/解禁_ @用户`**：管理员手动解禁。
- **命令 `/拉黑列表_`**：管理员查看当前黑名单。
- 管理员判定完全使用 **AstrBot 自带的管理员体系**（`admins_id`），插件不再维护独立的管理员名单。

### 🗣️ 永久拉黑自定义语录

- 当用户被 **永久拉黑** 后，每次尝试触发 Bot 时，如果距离上次自动回复超过配置间隔（默认 1 小时），Bot 会随机抽取一条自定义语录发回给他。
- 语录可在 WebUI 或 JSON 配置中编辑，支持列表随机抽取。
- 支持 **好友专用语录**：如果目标是 Bot 的 QQ 好友，可配置单独的语录池。
- 支持模板变量：
  - `{user_id}`：被拉黑用户 ID
  - `{duration}`：拉黑时长（永久时显示 `永久`）
  - `{ban_time}`：拉黑开始时间，如 `2026-06-25 14:30`
- 默认内置几条阴阳怪气语录，例如：
  - `您已被拉黑 {user_id}，已拉黑 {duration}。`
  - `被拉黑还锲而不舍地戳 Bot，建议输入 /删除bot 或自行删除 Bot 好友，对大家都好~`
  - `您已被永久拉黑，请继续表演，反正 Bot 不会再理你了。`
  - `黑名单里的空气还好吗？{user_id} 同学。`
  - `低质量骚扰已触发永久屏蔽，您已收获 Bot 的沉默大礼包。`

### 👤 好友检测与自动删除好友（参考 HappyBirthday 插件）

- 检测被永久拉黑的目标是否为 Bot 的 QQ 好友。
- 配置项 `auto_delete_friend_on_permanent_ban` 默认关闭；开启后，永久拉黑好友时会自动调用 OneBot 接口删除好友。
- 非好友用户只执行永久拉黑，不会尝试删除好友。
- 适配器主要面向 **aiocqhttp / OneBot 协议端**（NapCat、LLOneBot、go-cqhttp 等），如协议端不支持 `delete_friend` 会自动跳过并记录日志。

### 🚫 全局命令拦截

- 被拉黑用户发送的任何消息（包括 `/` 命令、@Bot、私聊等）都会在最早阶段被 `stop_event()` 拦截。
- 这意味着被拉黑者无法再用 `/draw` 等画图命令或其他插件指令继续骚扰 Bot。
- 永久拉黑用户被拦截时，仍会按间隔收到自定义语录。

### 📖 已读不回

- Bot 判断无需回复时，可调用 `read_and_ignore` 工具实现真正的已读不回，不发送任何消息。
- 每次已读不回操作会被记录，下次 Bot 被触发时会在系统提示中注入历史记录，帮助其判断是否继续保持沉默。
- 适用于防 Bot 互聊死循环、无意义回复、图片 spam 等场景。

## 🔧 配置项（WebUI 可调）

在 AstrBot Web 面板中配置以下参数：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `default_blacklist_duration` | int | `5` | 默认拉黑时长（分钟） |
| `ignore_cooldown` | int | `120` | 已读不回冷却时间（秒） |
| `enable_auto_spam_blacklist` | bool | `true` | 是否启用自动刷屏检测 |
| `spam_window_seconds` | int | `60` | 检测连续发消息的时间窗口（秒） |
| `spam_threshold` | int | `5` | 窗口内达到此连续次数即自动拉黑 |
| `auto_blacklist_duration_minutes` | int | `10` | 自动检测触发的拉黑时长（分钟） |
| `permanent_ban_messages` | list | 见上 | 永久拉黑自动回复语录列表，支持模板变量 |
| `friend_permanent_ban_messages` | list | `[]` | 好友专用永久拉黑语录，为空则使用通用语录 |
| `permanent_ban_reply_interval` | int | `3600` | 永久拉黑自动回复间隔（秒），默认 1 小时 |
| `auto_delete_friend_on_permanent_ban` | bool | `false` | 永久拉黑好友时是否自动删除好友（默认关闭） |
| `friend_list_refresh_interval` | int | `3600` | 好友列表缓存刷新间隔（秒） |

> 管理员身份由 AstrBot 全局配置 `admins_id` 决定，插件不再单独维护管理员名单。请在 AstrBot WebUI 的「系统设置」或 `cmd_config.json` 中配置管理员 ID。

## 🛠️ LLM 工具说明

### `ban_user`

拉黑指定用户，**仅管理员可调用**。

- 参数 `target_user_id`：目标用户 ID（群聊中也可通过 @ 目标自动识别，但建议填写）。
- 参数 `duration_minutes`：拉黑时长（分钟），`-1` 表示永久拉黑，默认永久。
- 参数 `reason`：拉黑原因，可选。

示例参数：

```json
{
  "target_user_id": "123456789",
  "duration_minutes": -1,
  "reason": "恶俗骚扰"
}
```

### `read_and_ignore`

已读不回工具，适用场景：

- 对方可能是另一个机器人，双方陷入无意义的循环对话
- 对方反复发送相似、重复、无意义的内容（含图片 spam）
- 当前对话已自然结束，继续回复只会没完没了
- 对方的消息确实不需要任何回应

调用时可传入 `reason` 参数记录原因。

### `reset_ignore_status`

重置已读不回状态。

## 📋 权限逻辑

- **管理员**：不受拉黑、已读不回、自动刷屏限制；可调用 `ban_user` 工具或 `/拉黑_` 命令拉黑任意用户（含永久）。
- **普通用户**：
  - 无法调用拉黑工具或命令；
  - 刷屏会被自动拉黑；
  - 被拉黑后，所有消息（含 `/` 命令）都会被拦截；
  - 永久拉黑后会按间隔收到自定义语录。

## 📦 安装 & 更新

在 AstrBot 插件市场搜索 `astrbot_plugin_LLMTempBan` 安装，或手动将插件目录放置到 AstrBot 的 `data/plugins` 下。

更新后建议在 WebUI 插件管理处 **重载插件**。

## 🧪 本地测试 & 兼容性确认

- 参考 AstrBot 官方 wiki：
  - [https://docs.astrbot.app/dev/star/plugin-new.html](https://docs.astrbot.app/dev/star/plugin-new.html)
  - [https://docs.astrbot.app/dev/star/guides/listen-message-event.html](https://docs.astrbot.app/dev/star/guides/listen-message-event.html)
  - 官方黑名单插件示例、recall_cancel 源码
- 管理员判定使用 `event.is_admin()`，与 AstrBot 自带 `admins_id` 保持一致。
- 命令使用 `@filter.permission_type(filter.PermissionType.ADMIN)` 由 AstrBot 统一鉴权。
- 使用 `event.stop_event()` 阻止 LLM 调用和后续处理，符合 recall_cancel / 官方黑名单插件实践。
- 在 on_llm_request 钩子中如需发送消息，使用 `event.send()` 直接发送（官方文档明确说明钩子中不能 yield）。
- 图片处理使用现有 `message_components.Image`，无额外依赖。
- 支持多平台（aiocqhttp 等），spam 检测通用。

有问题欢迎提 issue 或 PR。

## 变更日志

### v2.4.0

- 改用 AstrBot 自带管理员体系（`event.is_admin()` / `filter.PermissionType.ADMIN`），移除插件内 `administrators` 配置。
- LLM 工具改名为 `ban_user`，默认永久拉黑，描述更清晰，避免 LLM 误解为仅临时拉黑。
- 移除普通用户自助拉黑、反拉黑保护等非核心功能。
- 新增永久拉黑自定义自动回复语录：支持列表随机抽取、模板变量、可配置回复间隔（默认 1 小时）。
- 新增好友检测：参考 HappyBirthday 插件，可检测被永久拉黑目标是否为 Bot 好友；开启 `auto_delete_friend_on_permanent_ban` 后可自动删除好友（默认关闭）。
- 新增好友专用永久拉黑语录 `friend_permanent_ban_messages`。
- 被拉黑用户（含永久/临时）的所有消息都会被全局拦截，包括 `/` 命令。
- 修复原 `_get_image_identifier` 缩进问题，提升稳定性。
- 完善 `_conf_schema.json` 与 `metadata.yaml`，版本统一为 v2.4.0。

### v2.1.0

- 新增自动刷屏检测 + 图片去重 spam 优化。
- 支持窗口内连续 y 次 x 秒自动拉黑 z 分钟 + 持续发延长惩罚。
- 触发时尽早 stop_event 撤销 LLM。
- 完善 `_conf_schema.json`、metadata、README。

## License

AGPL-3.0 (同原)
