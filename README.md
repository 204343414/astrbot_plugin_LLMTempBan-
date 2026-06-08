# astrbot_plugin_LLMTempBan（增强版 v2.1.0）

一个为 AstrBot 设计的 LLM 临时拉黑屏蔽插件，支持管理员拉黑、普通用户自助拉黑、反拉黑保护、已读不回功能，以及**新增自动刷屏/图片spam检测**，有效防止 Bot 与 Bot 之间无限循环对话，以及傻13无脑发图烧AI token。

> **参考了 [astrbot_plugin_recall_cancel](https://github.com/muyouzhi6/astrbot_plugin_recall_cancel) 的代码风格和钩子使用**（on_llm_request 尽早拦截 + stop_event() + 日志），先查阅了 AstrBot wiki（plugin-new、listen-message-event、event hooks 如 on_llm_request / on_llm_response / event_message_type.ALL 等）和官方示例确认兼容性，无问题后再实现。所有钩子使用默认或合理优先级，不会干扰其他插件。

## ✨ 功能概览

### 🚫 临时拉黑（原有 + 增强）
- **管理员拉黑**：管理员可通过 @目标用户 将其临时拉黑，被拉黑用户在指定时间内无法触发 LLM 回复
- **普通用户自助拉黑**：普通用户可以拉黑自己（适用于主动屏蔽 Bot 回复的场景）
- **反拉黑保护**：普通用户尝试拉黑管理员时，会被反向拉黑自己，时长至少 5 分钟
- **Bot 自动拉黑**：Bot 可自动拉黑违规用户，管理员不受自动拉黑限制
- **自动过期清理**：拉黑到期后自动解除，无需手动操作
- **新增：自动刷屏检测**：一旦检测到同一用户在 **x秒内连续 y次主动向bot发消息**（仅统计到达 on_llm_request 的“主动”触发，即私聊或群@唤醒等），自动触发拉黑 **z分钟**。
  - 触发后**立即 event.stop_event() 撤销 LLM 调用**，避免烧 token（参考 recall_cancel 风格尽早拦截）。
  - **持续发消息就继续增大拉黑时间**（惩罚机制，像加数值的按钮玩具）。
  - 仅对非管理员生效。

### 📖 已读不回（原有）
- **LLM 主动调用**：Bot 判断无需回复时，可调用 `read_and_ignore` 工具实现真正的已读不回，不发送任何消息
- **历史记录注入**：每次已读不回操作会被记录，下次 Bot 被触发时会在系统提示中看到历史记录，帮助其判断是否继续保持沉默
- **防 Bot 互聊死循环**：当两个 Bot 陷入无意义的循环客套时，Bot 能识别并主动终止对话

### 🖼️ 图片同一张优化（新增）
- **算法优化**：同一消息内相同图片直接**简化计数**（使用 file/url 字段做高效字符串 key 去重，无需下载/哈希，最高效）。
  - 本消息内有重复相同图片 → 额外惩罚积分（加速触发自动拉黑）。
  - 跨消息重复发同一张图片 → 正常累积窗口计数（聊天里不会“显示”第二次的感觉，简化处理）。
- 发图 spam 会被更快检测到（独特图片越多积分越高 + 重复惩罚）。

## 🔧 配置项（WebUI 可调）

在 AstrBot Web 面板中配置以下参数（新增了完整 schema）：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `administrators` | list | `[]` | 管理员用户 ID 列表，管理员不受拉黑和已读不回限制 |
| `default_blacklist_duration` | int | `5` | 默认拉黑时长（分钟） |
| `ignore_cooldown` | int | `120` | 已读不回冷却时间（秒） |
| `enable_auto_spam_blacklist` | bool | `true` | 是否启用自动刷屏检测 |
| `spam_window_seconds` | int | `60` | 检测连续发消息的时间窗口（秒） |
| `spam_threshold` | int | `5` | 窗口内达到此连续次数即自动拉黑 |
| `auto_blacklist_duration_minutes` | int | `10` | 自动检测触发的拉黑时长（分钟） |

> Bot 首次收到消息时会自动将自身 ID 加入管理员列表并持久化保存。

## 🛠️ LLM 工具说明

### `add_temporary_blacklist`
拉黑工具，LLM 根据对话上下文判断并调用：

- 管理员调用：拉黑 @指定的目标用户
- 普通用户调用：仅能拉黑自己，尝试拉黑管理员会被反向拉黑
- 可指定 `duration_minutes` 参数自定义时长

### `read_and_ignore`
已读不回工具，适用场景：

- 对方可能是另一个机器人，双方陷入无意义的循环对话
- 对方反复发送相似、重复、无意义的内容（含图片spam）
- 当前对话已自然结束，继续回复只会没完没了
- 对方的消息确实不需要任何回应

调用时可传入 `reason` 参数记录原因，每个会话保留最近 50 条历史记录。

### `reset_ignore_status`
重置已读不回状态。

## 📋 权限逻辑

```
管理员：
  ├── 不受拉黑限制
  ├── 不受已读不回影响
  ├── 可拉黑任意非管理员用户
  └── 不会被自动拉黑

普通用户：
  ├── 可拉黑自己
  ├── 不能拉黑其他普通用户
  └── 尝试拉黑管理员 → 反向拉黑自己
  └── 刷屏 → 自动拉黑（可延长）
```

## 📦 安装 & 更新

在 AstrBot 插件市场搜索 `astrbot_plugin_LLMTempBan` 安装，或手动将插件目录放置到 AstrBot 的 `data/plugins` 下。

更新后建议在 WebUI 插件管理处 **重载插件**。

## 🧪 本地测试 & 兼容性确认

- 参考 AstrBot 官方 wiki：
  - https://docs.astrbot.app/dev/star/plugin-new.html
  - https://docs.astrbot.app/dev/star/guides/listen-message-event.html （event hooks、on_llm_request、stop_event、priority 等）
  - 官方黑名单插件示例、recall_cancel 源码
- 确认使用 `@filter.on_llm_request()` 是安全的（当前插件已在用，官方 pipeline 支持多个 hook 顺序执行）。
- 使用 `event.stop_event()` 阻止 LLM 调用和后续处理，符合 recall_cancel / 官方 blacklist 插件实践。
- 图片处理使用现有 message_components.Image，无额外依赖。
- 支持多平台（aiocqhttp 等），spam 检测通用。
- 所有修改均为向后兼容增强。

有问题欢迎提 issue 或 PR。

## 变更日志 (v2.1.0)

- 新增自动刷屏检测 + 图片去重 spam 优化（结合用户需求 + recall_cancel 参考）
- 支持窗口内连续 y 次 x 秒自动拉黑 z 分钟 + 持续发延长惩罚
- 触发时尽早 stop_event 撤销 LLM
- 完善 _conf_schema.json、metadata、README
- 最高效图片 key 算法（file/url 字符串匹配 + 单消息去重简化）
- 控制台详细日志（无用户通知，静默拉黑）

## License

AGPL-3.0 (同原)
