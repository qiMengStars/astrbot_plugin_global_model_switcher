# astrbot_plugin_global_model_switcher

适用于 [AstrBot](https://github.com/Soulter/AstrBot) 的全局默认大语言模型（LLM Provider）动态切换插件。

AstrBot 内置的 `/provider` 指令仅切换当前会话（UMO）绑定的模型，无法修改全局默认配置。本插件允许管理员在聊天中通过指令直接热切换全局默认模型并持久化到配置文件，供全局对话以及未单独指定 Provider 的后台任务（如群分析插件、定时任务）即时采用。

> [!NOTE]
> 本项目包含极高成分vibe coding

## 功能特性

- **查看列表 (`/gmls`)**：列出所有已启用的对话模型 Provider、对应模型名称与服务类型，并标记当前全局生效项。
- **热切换 (`/gmsw`)**：支持通过列表序号或 Provider ID 切换全局默认模型，修改即时生效并保存到配置文件。
- **安全机制**：调用 AstrBot 自带的配置保存接口并检查返回值；保存或后续步骤失败时，按深拷贝快照还原配置并重新保存，回滚保存失败会明确提示。
- **权限控制**：指令仅限管理员可用。

## 指令说明

| 指令 | 权限 | 说明 | 示例 |
| :--- | :--- | :--- | :--- |
| `/gmls` | 管理员 | 查看当前已启用的 LLM Provider 列表及全局默认项 | `/gmls` |
| `/gmsw <序号或ID>` | 管理员 | 切换全局默认 LLM Provider 并持久化保存 | `/gmsw 1` 或 `/gmsw deepseek_chat` |

### 输出示例

```text
🤖 【AstrBot 全局可用 LLM Provider 列表】
------------------------------------
* [1] openai_official (当前全局默认)
      ├─ 模型: gpt-4o / gpt-4o-mini
      └─ 类型: openai
  [2] deepseek_chat
      ├─ 模型: deepseek-chat
      └─ 类型: openai_compatible
------------------------------------
📌 当前全局生效 ID: openai_official
💡 快捷切换方式: /gmsw <序号或ID>
```

## 使用提示

- **匹配优先级**：`/gmsw` 先按 Provider ID 精确匹配（忽略大小写），未匹配且输入为纯数字时再按列表序号解析。因此纯数字命名的 Provider ID 可以直接用 ID 切换。
- **后台与跨插件任务**：依赖全局默认模型的插件（如群分析插件）或定时任务，只有在其配置中的 Provider 项保持留空、且对应会话没有用 `/provider` 单独指定模型时，切换全局模型后才会自动跟随。
- **生效范围**：只修改 AstrBot 的 default 配置，不会改动会话级 profile 或 `/provider` 产生的会话偏好。配置存在 `agent` 段时写入 `agent.config.model.provider_id` 并同步 `provider_settings.default_provider_id`（兼容 4.5.x），否则写入 `provider_settings.default_provider_id`；`agent.runtime` 不是 `astrbot` 时切换会被拒绝。
- **群分析插件**：其 `llm_provider_id` 以及 `topic_provider_id`、`user_title_provider_id`、`golden_quote_provider_id` 等专用项需要留空，否则它会显式指定 Provider，绕过全局默认。

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
