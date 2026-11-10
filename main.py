import asyncio
import copy
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

logger = logging.getLogger(__name__)

NON_CHAT_PROVIDER_TYPES: Tuple[str, ...] = (
    "embedding",
    "text_to_speech",
    "speech_to_text",
    "rerank",
    "moderation",
    "image_generation",
    "image_to_text",
    "tts",
    "stt",
)

# 配置文件里不存在的键用该哨兵表示，回滚时据此删除本次新增的键。
_MISSING = object()

# 插件只会改动这两个子树，快照与回滚都以它们为单位。
_CONFIG_KEYS: Tuple[str, ...] = ("agent", "provider_settings")


def _extract_models(models_field: Any, model_field: Any) -> List[str]:
  """从 Provider 配置项中提取有效模型名称列表。"""
  if isinstance(models_field, list):
    return [str(m).strip() for m in models_field if str(m).strip()]
  if isinstance(models_field, str) and models_field.strip():
    return [models_field.strip()]
  if isinstance(model_field, str) and model_field.strip():
    return [model_field.strip()]
  if isinstance(model_field, list):
    return [str(m).strip() for m in model_field if str(m).strip()]
  return []


def _find_target_provider(
    target: str, providers: List[Dict[str, Any]]
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
  """根据用户输入的 Provider ID 或序号查找目标 Provider。"""
  if not providers:
    return None, "当前没有已启用的 Provider 可供切换。"

  matched = [p for p in providers if p["id"] == target]
  if matched:
    return matched[0], None

  ci_matched = [p for p in providers if p["id"].lower() == target.lower()]
  if ci_matched:
    return ci_matched[0], None

  if target.isdigit():
    idx = int(target)
    if 1 <= idx <= len(providers):
      return providers[idx - 1], None
    return None, (
        f"序号超出范围，有效序号为 1 到 {len(providers)}。发送 /gmls 查看列表。"
    )

  return None, f"未找到 ID 为 {target} 的可用 Provider。发送 /gmls 查看列表。"


def _build_success_message(
    target_p: Dict[str, Any], old_id: Optional[str]
) -> str:
  """生成切换成功的提示文本。"""
  p_id = target_p["id"]
  models = target_p.get("models", [])
  models_str = " / ".join(models) if models else "默认模型"
  p_type = target_p.get("type", "未知")

  return "\n".join([
      "全局默认 Provider 已切换",
      f"原默认 ID: {old_id or '未显式指定'}",
      f"新默认 ID: {p_id}",
      f"模型: {models_str}",
      f"类型: {p_type}",
  ])


def _extract_command_target(target: Any) -> str:
  """清洗 AstrBot 依 handler 签名注入的指令参数。"""
  if target is None:
    return ""
  return str(target).strip()


def _extract_id_from_model_dict(model_cfg: Any) -> str:
  """从 model 子字典中提取 provider ID。"""
  if not isinstance(model_cfg, dict):
    return ""
  p_id = model_cfg.get("provider_id") or model_cfg.get("default_provider_id")
  return p_id.strip() if isinstance(p_id, str) and p_id.strip() else ""


def _extract_id_from_agent_section(cfg: Dict[str, Any], key: str) -> str:
  """从 agent 配置段中提取默认 model ID。"""
  section = cfg.get(key)
  if not isinstance(section, dict):
    return ""
  sub = section.get("config")
  if not isinstance(sub, dict):
    return ""
  return _extract_id_from_model_dict(sub.get("model"))


def _extract_id_from_cfg(cfg: Any) -> str:
  """从配置对象中提取当前生效的默认对话模型 ID。

  现代 AstrBot 的解析器只读 agent.config.model.provider_id，旧版字段只在没有 agent 段时使用。
  """
  if not isinstance(cfg, dict):
    return ""

  agent_section = cfg.get("agent")
  if isinstance(agent_section, dict):
    # agent 段存在时它是权威来源；即使 provider_id 为空也不能回退到旧键，
    # 否则 WebUI 清空默认项后插件会报告一个早已失效的 ID。
    runtime = agent_section.get("runtime")
    if runtime and runtime != "astrbot":
      return ""
    return _extract_id_from_agent_section(cfg, "agent")

  ps = cfg.get("provider_settings")
  if isinstance(ps, dict):
    p_id = ps.get("default_provider_id")
    if isinstance(p_id, str) and p_id.strip():
      return p_id.strip()

  top_id = cfg.get("default_provider_id") or cfg.get("chat_model")
  if isinstance(top_id, str) and top_id.strip():
    return top_id.strip()

  return ""


def _apply_config_model_change(cfg: Any, target_id: str) -> bool:
  """把默认 Provider 写入当前配置布局的权威键。

  AstrBot 4.5.x 的 ProviderManager 读 provider_settings.default_provider_id，
  较新版本改读 agent.config.model.provider_id。存在 agent 段时按新布局写入，
  并同步旧键以便降级回 4.5.x 后仍然生效；否则按旧布局写入。Agent 运行时不是
  astrbot 时该配置不参与对话解析，直接报错而不是静默失效。
  """
  if not isinstance(cfg, dict):
    return False

  ps = cfg.get("provider_settings")
  agent = cfg.get("agent")
  if not isinstance(agent, dict) and not isinstance(ps, dict):
    return False

  if isinstance(agent, dict):
    runtime = agent.get("runtime")
    if runtime and runtime != "astrbot":
      raise RuntimeError(
          f"当前 Agent 运行时为「{runtime}」，全局默认 Provider 仅对 astrbot 运行时生效"
      )
    if not runtime:
      agent["runtime"] = "astrbot"

    agent_config = agent.get("config")
    if not isinstance(agent_config, dict):
      agent_config = {}
      agent["config"] = agent_config

    model_cfg = agent_config.get("model")
    if not isinstance(model_cfg, dict):
      model_cfg = {}
      agent_config["model"] = model_cfg

    model_cfg["provider_id"] = target_id
    if "default_provider_id" in model_cfg:
      model_cfg["default_provider_id"] = target_id

  if isinstance(ps, dict):
    ps["default_provider_id"] = target_id

  return True


def _snapshot_single_config(cfg: Any) -> Dict[str, Any]:
  """深拷贝会被修改的配置子树，供失败时完整还原。"""
  snapshot: Dict[str, Any] = {}
  if not isinstance(cfg, dict):
    return snapshot
  for key in _CONFIG_KEYS:
    snapshot[key] = copy.deepcopy(cfg[key]) if key in cfg else _MISSING
  return snapshot


def _restore_single_config(cfg: Any, snapshot: Dict[str, Any]) -> None:
  """按快照整体恢复配置子树，并删除本次新增的键。"""
  if not isinstance(cfg, dict):
    return
  for key, value in snapshot.items():
    if value is _MISSING:
      cfg.pop(key, None)
    else:
      cfg[key] = copy.deepcopy(value)


@register(
    "astrbot_plugin_global_model_switcher",
    "xixi",
    "AstrBot 全局 LLM Provider 切换插件",
    "1.0.7",
    "https://github.com/xixil/astrbot_plugin_global_model_switcher",
)
class GlobalModelSwitcherPlugin(Star):
  """AstrBot 全局默认 LLM Provider 切换插件。"""

  def __init__(
      self,
      context: Context,
      config: Optional[Dict[str, Any]] = None,
  ):
    super().__init__(context)
    self.config = config or {}
    self._lock = asyncio.Lock()
    logger.info("[GlobalModelSwitcher] 全局模型切换插件已加载 (v1.0.7)")

  def _get_global_config(self) -> Optional[Any]:
    """获取 AstrBot 的全局默认配置对象，不回退到会话 profile。"""
    cfg = getattr(self.context, "_config", None)
    if isinstance(cfg, dict):
      return cfg

    for mgr_name in ("astrbot_config_mgr", "config_manager", "_config_manager", "acm"):
      mgr = getattr(self.context, mgr_name, None)
      if not mgr:
        continue
      default_conf = getattr(mgr, "default_conf", None)
      if isinstance(default_conf, dict):
        return default_conf
      confs = getattr(mgr, "confs", None)
      if isinstance(confs, dict) and isinstance(confs.get("default"), dict):
        return confs["default"]

    return None

  def _is_chat_provider(self, provider_type: str) -> bool:
    """判定 Provider 是否为可用于对话生成的文本模型服务。"""
    normalized_type = provider_type.strip().lower()
    if not normalized_type or normalized_type == "agent_runner":
      return False
    return not any(
        non_chat in normalized_type for non_chat in NON_CHAT_PROVIDER_TYPES
    )

  def _parse_single_config_provider(
      self, item: Any, seen_ids: Set[str], disabled_ids: Set[str]
  ) -> Optional[Dict[str, Any]]:
    """解析单个配置中的 Provider 条目。"""
    if not isinstance(item, dict):
      return None
    raw_id = item.get("id") or item.get("provider_id")
    p_id = str(raw_id).strip() if raw_id else ""
    if not p_id:
      return None
    if not item.get("enable", True):
      disabled_ids.add(p_id)
      return None
    p_type = str(item.get("provider_type") or item.get("type") or "unknown")
    if not self._is_chat_provider(p_type):
      return None
    model_config = item.get("model_config")
    model_name = item.get("model")
    if not model_name and isinstance(model_config, dict):
      model_name = model_config.get("model")
    seen_ids.add(p_id)
    return {
        "id": p_id,
        "type": p_type,
        "models": _extract_models(item.get("models"), model_name),
        "enable": True,
    }

  def _parse_config_providers(
      self, seen_ids: Set[str], disabled_ids: Set[str]
  ) -> List[Dict[str, Any]]:
    """从全局配置中提取所有启用的对话模型 Provider。"""
    cfg = self._get_global_config()
    if cfg is None:
      return []

    raw = cfg.get("provider") or cfg.get("providers") or []
    if isinstance(raw, list):
      items = raw
    elif isinstance(raw, dict):
      items = list(raw.values())
    else:
      items = []

    result: List[Dict[str, Any]] = []
    for item in items:
      parsed = self._parse_single_config_provider(item, seen_ids, disabled_ids)
      if parsed:
        result.append(parsed)
    return result

  def _get_runtime_instances(self) -> List[Any]:
    """获取运行时 Provider 实例列表。"""
    get_all = getattr(self.context, "get_all_providers", None)
    if callable(get_all):
      res = get_all()
      if isinstance(res, list):
        return res
      if res:
        return list(res)

    pm = getattr(self.context, "provider_manager", None)
    inst_map = getattr(pm, "inst_map", None)
    if isinstance(inst_map, dict):
      return list(inst_map.values())

    return []

  def _extract_runtime_p_id(self, meta: Any, p: Any, p_cfg: Dict[str, Any]) -> str:
    """解析运行时 Provider 实例的标识符。"""
    raw_id = (
        getattr(meta, "id", None)
        or getattr(p, "provider_id", None)
        or getattr(p, "id", None)
        or p_cfg.get("id")
        or p_cfg.get("provider_id")
    )
    return str(raw_id).strip() if raw_id else ""

  def _extract_runtime_p_type(self, meta: Any, p: Any, p_cfg: Dict[str, Any]) -> str:
    """解析运行时 Provider 实例的服务类型。"""
    raw_type = (
        getattr(meta, "type", None)
        or getattr(p, "provider_type", None)
        or getattr(p, "type", None)
        or p_cfg.get("provider_type")
        or p_cfg.get("type")
        or "chat_completion"
    )
    value = getattr(raw_type, "value", raw_type)
    return str(value)

  def _parse_single_runtime_provider(
      self, p: Any, seen_ids: Set[str], disabled_ids: Set[str]
  ) -> Optional[Dict[str, Any]]:
    """解析单个运行时 Provider 实例。"""
    meta = p.meta() if callable(getattr(p, "meta", None)) else None
    p_cfg = getattr(p, "provider_config", None) or {}

    clean_id = self._extract_runtime_p_id(meta, p, p_cfg)
    if not clean_id:
      return None

    is_enabled = getattr(p, "enable", None)
    if is_enabled is None:
      is_enabled = p_cfg.get("enable", True)

    if clean_id in seen_ids or clean_id in disabled_ids or not is_enabled:
      return None

    p_type = self._extract_runtime_p_type(meta, p, p_cfg)
    if not self._is_chat_provider(p_type):
      return None

    model_config = p_cfg.get("model_config")
    model_name = p_cfg.get("model")
    if not model_name and isinstance(model_config, dict):
      model_name = model_config.get("model")
    seen_ids.add(clean_id)
    return {
        "id": clean_id,
        "type": p_type,
        "models": _extract_models(p_cfg.get("models"), model_name),
        "enable": True,
    }

  def _parse_runtime_providers(
      self, seen_ids: Set[str], disabled_ids: Set[str]
  ) -> List[Dict[str, Any]]:
    """从运行时 ProviderManager 补充未收录的已启用对话模型实例。"""
    result: List[Dict[str, Any]] = []
    for p in self._get_runtime_instances():
      parsed = self._parse_single_runtime_provider(p, seen_ids, disabled_ids)
      if parsed:
        result.append(parsed)
    return result

  def get_enabled_providers(self) -> List[Dict[str, Any]]:
    """获取系统中所有已启用的对话型 LLM Provider 列表。"""
    seen_ids: Set[str] = set()
    disabled_ids: Set[str] = set()
    providers: List[Dict[str, Any]] = []
    try:
      providers.extend(self._parse_config_providers(seen_ids, disabled_ids))
    except Exception as e:
      logger.warning(
          f"[GlobalModelSwitcher] 读取配置 provider 列表异常: {e}", exc_info=True
      )
    try:
      providers.extend(self._parse_runtime_providers(seen_ids, disabled_ids))
    except Exception as e:
      logger.warning(
          f"[GlobalModelSwitcher] 读取运行时 provider 列表异常: {e}", exc_info=True
      )
    return providers

  def get_current_default_provider_id(self) -> str:
    """获取当前全局生效的 default Provider ID。

    配置未显式指定时，AstrBot 会回退到第一个已加载的 chat provider。
    """
    detected_id = _extract_id_from_cfg(self._get_global_config())
    if detected_id:
      return detected_id

    for p in self._get_runtime_instances():
      meta = p.meta() if callable(getattr(p, "meta", None)) else None
      p_cfg = getattr(p, "provider_config", None) or {}
      if self._is_chat_provider(self._extract_runtime_p_type(meta, p, p_cfg)):
        return self._extract_runtime_p_id(meta, p, p_cfg)
    return ""

  def format_provider_list(
      self, providers: List[Dict[str, Any]], current_default_id: str
  ) -> str:
    """格式化 Provider 列表为可读文本消息。"""
    if not providers:
      return "未检测到已启用的对话型 LLM Provider，请先在 WebUI 的「服务提供商」中配置并启用。"

    lines: List[str] = ["全局可用 LLM Provider 列表"]
    for idx, p in enumerate(providers, start=1):
      p_id = p["id"]
      models = p.get("models", [])
      models_str = " / ".join(models) if models else "默认模型"
      is_current = p_id == current_default_id
      status_tag = " (当前全局默认)" if is_current else ""
      lines.append(f"[{idx}] {p_id}{status_tag}  模型: {models_str}  类型: {p.get('type', '未知')}")

    lines.extend([
        f"当前全局生效 ID: {current_default_id or '未显式指定'}",
        "切换方式: /gmsw <序号或ID>，例如 /gmsw 1 或 /gmsw deepseek_chat",
    ])
    return "\n".join(lines)

  async def _save_config_async(self, config: Any) -> None:
    """调用配置对象自带的持久化接口，保留其原子写与版本校验。"""
    if not isinstance(config, dict):
      raise RuntimeError("无法获取 AstrBot 配置对象，无法保存")

    save_async = getattr(config, "save_config_async", None)
    if callable(save_async):
      result = await save_async()
      if result is False:
        raise RuntimeError("save_config_async 返回 False，配置未持久化")
      return

    save_sync = getattr(config, "save_config", None)
    if callable(save_sync):
      result = await asyncio.to_thread(save_sync)
      if result is False:
        raise RuntimeError("save_config 返回 False，配置未持久化")
      return

    raise RuntimeError("当前配置对象不支持保存（缺少 save_config_async/save_config）")

  def _get_default_target_configs(self) -> List[Any]:
    """返回全局默认配置对象，不包含任何会话级 profile。"""
    cfg = self._get_global_config()
    return [cfg] if isinstance(cfg, dict) else []

  async def _save_all_configs_async(self, configs: List[Any]) -> None:
    """保存被修改的配置对象，按配置文件路径去重。"""
    saved_keys: Set[Any] = set()
    for cfg in configs:
      config_path = getattr(cfg, "config_path", None)
      key = config_path or id(cfg)
      if key in saved_keys:
        continue
      await self._save_config_async(cfg)
      saved_keys.add(key)

  def _create_state_snapshot(self, configs: List[Any], pm: Any) -> Dict[str, Any]:
    """生成用于异常安全回滚的状态快照。"""
    snapshot: Dict[str, Any] = {
        "configs": [_snapshot_single_config(c) for c in configs],
        "pm_default_chat_id": getattr(pm, "default_chat_provider_id", None),
        "curr_inst": getattr(pm, "curr_provider_inst", None),
    }
    return snapshot

  def _sync_runtime_manager(self, pm: Any, target_id: str) -> None:
    """同步 ProviderManager 的兼容字段，供仍读取旧属性的代码使用。"""
    if not pm:
      return
    if hasattr(pm, "default_chat_provider_id"):
      pm.default_chat_provider_id = target_id
    inst_map = getattr(pm, "inst_map", None)
    if isinstance(inst_map, dict) and target_id in inst_map:
      pm.curr_provider_inst = inst_map[target_id]

  async def _rollback_state(
      self, configs: List[Any], pm: Any, snapshot: Dict[str, Any]
  ) -> bool:
    """回滚配置与 ProviderManager；返回是否成功重新保存。"""
    for cfg, snap in zip(configs, snapshot.get("configs", [])):
      _restore_single_config(cfg, snap)

    if pm:
      if hasattr(pm, "default_chat_provider_id"):
        pm.default_chat_provider_id = snapshot.get("pm_default_chat_id")
      if hasattr(pm, "curr_provider_inst"):
        pm.curr_provider_inst = snapshot.get("curr_inst")

    try:
      await self._save_all_configs_async(configs)
    except Exception as rb_err:
      logger.critical(
          f"[GlobalModelSwitcher] 配置回滚保存失败: {rb_err}", exc_info=True
      )
      return False
    return True

  @filter.permission_type(filter.PermissionType.ADMIN)
  @filter.command("gmls")
  async def gmls(self, event: AstrMessageEvent):
    """列出当前所有已启用的 LLM Provider 及其模型信息，并标记全局默认项。"""
    providers = self.get_enabled_providers()
    current_default = self.get_current_default_provider_id()
    yield event.plain_result(
        self.format_provider_list(providers, current_default)
    )

  async def _execute_model_switch(
      self, event: AstrMessageEvent, target_p: Dict[str, Any]
  ):
    """切换配置并持久化，失败时回滚。"""
    target_id = target_p["id"]
    async with self._lock:
      pm = getattr(self.context, "provider_manager", None)
      inst_map = getattr(pm, "inst_map", None)
      if isinstance(inst_map, dict) and target_id not in inst_map:
        yield event.plain_result(
            f"Provider {target_id} 未加载成功，无法切换。请先在 WebUI 检查其配置并重载。"
        )
        return

      target_configs = self._get_default_target_configs()
      if not target_configs:
        yield event.plain_result("未能定位全局配置对象，无法切换。")
        return

      current_default = self.get_current_default_provider_id()
      if target_id == current_default:
        yield event.plain_result(f"当前全局默认 Provider 已是 {target_id}，无需切换。")
        return

      snapshot = self._create_state_snapshot(target_configs, pm)
      try:
        applied = [
            cfg
            for cfg in target_configs
            if _apply_config_model_change(cfg, target_id)
        ]
        if not applied:
          raise RuntimeError(
              "全局配置中缺少 provider_settings/agent 段，无法写入默认 Provider"
          )
        self._sync_runtime_manager(pm, target_id)
        await self._save_all_configs_async(target_configs)
        yield event.plain_result(_build_success_message(target_p, current_default))
      except Exception as e:
        logger.error(
            f"[GlobalModelSwitcher] 切换全局 Provider 失败，回滚配置: {e}",
            exc_info=True,
        )
        rollback_saved = await self._rollback_state(target_configs, pm, snapshot)
        if rollback_saved:
          rollback_hint = "配置已回滚并重新保存。"
        else:
          rollback_hint = "回滚后的保存同样失败，配置可能未恢复，请手动检查配置文件。"
        yield event.plain_result(
            f"切换全局 Provider 失败: {e}\n{rollback_hint}"
        )

  @filter.permission_type(filter.PermissionType.ADMIN)
  @filter.command("gmsw")
  async def gmsw(self, event: AstrMessageEvent, target: Optional[Any] = None):
    """热切换 AstrBot 全局默认 LLM Provider（支持指定序号或 Provider ID）。"""
    clean_target = _extract_command_target(target)
    if not clean_target:
      yield event.plain_result(
          "请指定目标 Provider 的序号或 ID，例如 /gmsw 1 或 /gmsw deepseek_chat，发送 /gmls 查看列表。"
      )
      return

    providers = self.get_enabled_providers()
    if not providers:
      yield event.plain_result("未检索到已启用的对话型 LLM Provider，无法切换。")
      return

    target_p, err_msg = _find_target_provider(clean_target, providers)
    if err_msg or not target_p:
      yield event.plain_result(err_msg or "未找到目标 Provider。")
      return

    async for result in self._execute_model_switch(event, target_p):
      yield result

  async def terminate(self):
    """插件卸载清理逻辑。"""
    logger.info("[GlobalModelSwitcher] 全局模型切换插件已卸载")
