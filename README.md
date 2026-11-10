# astrbot_plugin_global_model_switcher

适用于 [AstrBot](https://github.com/Soulter/AstrBot) 的全局默认 LLM Provider 切换插件。

AstrBot 内置的 `/provider` 仅切换当前会话（UMO）绑定的模型，不影响全局默认配置。本插件提供管理员指令，用于切换全局默认 LLM Provider 并写入配置文件，供使用默认 Provider 的会话与后台任务采用。

## 功能特性

- `/gmls`：列出已启用的对话型 Provider 及其模型名称和服务类型，并标记当前全局默认项。
- `/gmsw <序号或ID>`：按列表序号或 Provider ID 切换全局默认 Provider，并写入配置文件。
- 失败回滚：保存接口返回失败时，按深拷贝快照还原配置并重新保存；回滚后的保存仍失败时在回复中说明。
- 权限控制：两条指令仅对管理员开放。

## 指令说明

| 指令 | 权限 | 说明 | 示例 |
| :--- | :--- | :--- | :--- |
| `/gmls` | 管理员 | 查看已启用的 LLM Provider 列表及当前全局默认项 | `/gmls` |
| `/gmsw <序号或ID>` | 管理员 | 切换全局默认 LLM Provider 并保存 | `/gmsw 1` 或 `/gmsw deepseek_chat` |

### 输出示例

```text
全局可用 LLM Provider 列表
[1] openai_official (当前全局默认)  模型: gpt-4o / gpt-4o-mini  类型: openai_chat_completion
[2] deepseek_chat  模型: deepseek-chat  类型: openai_chat_completion
当前全局生效 ID: openai_official
切换方式: /gmsw <序号或ID>，例如 /gmsw 1 或 /gmsw deepseek_chat
```

```text
全局默认 Provider 已切换
原默认 ID: openai_official
新默认 ID: deepseek_chat
模型: deepseek-chat
类型: openai_chat_completion
```

## 使用提示

- **匹配优先级**：`/gmsw` 先按 Provider ID 精确匹配（忽略大小写），未命中且参数为纯数字时按列表序号解析，因此纯数字命名的 Provider ID 也可按 ID 切换。
- **配置写入位置**：仅修改 AstrBot 的 default 配置，不修改会话级 profile，也不修改 `/provider` 写入的会话偏好。配置中存在 `agent` 段时写入 `agent.config.model.provider_id`，并同步 `provider_settings.default_provider_id` 以兼容 4.5.x；不存在 `agent` 段时仅写入后者。`agent.runtime` 不为 `astrbot` 时拒绝切换并返回错误。
- **后台任务与第三方插件**：仅在其配置中的 Provider 项留空，且对应会话未通过 `/provider` 指定模型时，才会采用全局默认 Provider。
- **群分析插件**：`llm_provider_id`、`topic_provider_id`、`user_title_provider_id`、`golden_quote_provider_id` 等配置项需留空，否则插件会显式指定 Provider，不采用全局默认值。

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
