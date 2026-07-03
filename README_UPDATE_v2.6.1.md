# v2.6.1 更新：被拉黑多次用户指令限制功能

## 新增配置项（WebUI 可直接编辑）

在 AstrBot WebUI 的插件配置页面中新增两个配置项：

### 1. `ban_count_threshold_for_restrict`（拉黑次数限制指令阈值）
- **类型**：int
- **默认值**：3
- **说明**：当用户累计被拉黑次数 ≥ 此值时，自动禁止使用下方配置的指令。

### 2. `ban_count_restrict_commands`（被拉黑多次后禁止使用的指令列表）
- **类型**：list（字符串列表）
- **默认值**：["draw", "image", "chat"]
- **说明**：填写指令名称（不含 `/`），例如 `draw`、`image`、`music`、`chat` 等。
  - 当用户被拉黑次数达到阈值时，这些指令将被拦截。
  - 支持在 WebUI 的列表表单中直接添加/删除指令。

**效果**：
- 在 `on_llm_request` 阶段自动注入警告到 LLM 上下文。
- Bot 会看到“该用户已被拉黑 X 次，已禁止使用以下指令：/draw、/image...”
- 同时仍保留原有的拉黑历史注入和 `leave_group` 功能。

## 使用示例

**推荐配置**：
- 阈值：`3`
- 禁止指令：`["draw", "image", "music", "video"]`

这样当某个用户被拉黑 3 次后，就无法再使用画图、音乐等容易被滥用的指令。

---

**已推送至 GitHub**：https://github.com/204343414/astrbot_plugin_LLMTempBan-
