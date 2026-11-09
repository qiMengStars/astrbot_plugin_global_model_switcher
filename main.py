import asyncio
import copy
import json
import os
from typing import Any, Dict, List, Optional, Set, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:
  from astrbot.api.star import register
except ImportError:

  def register(*args: Any, **kwargs: Any):
    def decorator(cls: Any) -> Any:
      return cls

    return decorator


NON_CHAT_PROVIDER_TYPES = {
    "speech_to_text",
    "text_to_speech",
    "stt",
    "tts",
    "embedding",
    "rerank",
    "image_generation",
    "drawing",
}


def _get_item_model_id(item: Any) -> Optional[str]:
  """提取配置条目中的模型名称。"""
  if isinstance(item, str):
    return item
  if isinstance(item, dict) and item.get("enable", True):
    for key in ("model_id", "name", "id", "model"):
      val = item.get(key)
      if val:
        return str(val)
  return None


def _extract_models(models_raw: Any, fallback_model: Any = None) -> List[str]:
  """提取配置或运行时实例中启用的模型名称列表。"""
  seen: Set[str] = set()
  result: List[str] = []

  items = models_raw if isinstance(models_raw, list) else [models_raw]
  for item in items:
    m_id = _get_item_model_id(item)
    cleaned = str(m_id).strip() if m_id else ""
    if cleaned and cleaned not in seen:
      seen.add(cleaned)
      result.append(cleaned)

  if not result and fallback_model is not None:
    if isinstance(fallback_model, (list, dict)):
      return _extract_models(fallback_model)
    fb_clean = str(fallback_model).strip()
    if fb_clean:
      result.append(fb_clean)

  return result


def _find_target_provider(
    target: str, providers: List[Dict[str, Any]]
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
  """按优先级匹配目标 Provider：精确 ID 匹配 -> 序号匹配 -> 大小写不敏感 ID 匹配。"""
  clean_target = target.strip()
  if not clean_target:
    return None, "⚠️ 请指定目标 LLM Provider 的序号或 ID。"

  for p in providers:
    if p["id"] == clean_target:
      return p, None

  if clean_target.isdigit():
    idx = int(clean_target)
    if 1 <= idx <= len(providers):
      return providers[idx - 1], None
    return (
        None,
        f"⚠️ 序号超出有效范围！当前有效序号范围为 1 ~ {len(providers)}。\n"
        "💡 可发送 /gmls 查看最新列表。",
    )

  target_lower = clean_target.lower()
  for p in providers:
    if p["id"].lower() == target_lower:
      return p, None

  return (
      None,
      f"⚠️ 未找到 ID 为「{clean_target}」的已启用对话模型 Provider！\n"
      "💡 请发送 /gmls 查看当前已启用的 Provider 列表。",
  )


def _build_success_message(target_p: Dict[str, Any], old_id: str) -> str:
  """构建模型切换成功的提示文本。"""
  models = target_p.get("models", [])
  models_str = " / ".join(models) if models else "默认模型"
  return "\n".join([
      "🎉 【全局默认 LLM Provider 切换成功】",
      "------------------------------------",
      f"🔄 原默认 ID : {old_id or '未显式指定（跟随首项）'}",
      f"🎯 新默认 ID : {target_p['id']}",
      f"🤖 对应模型  : {models_str}",
      f"🏷️ 服务类型  : {target_p.get('type', '未知')}",
      "------------------------------------",
      "💾 全局配置文件已持久化，重启保持有效。",
      "⚡ 内存热更新即时生效，无单独锁定模型的后台任务与插件（如群分析）将自动接入新模型。",
      "💡 提示：若群分析插件需跟随此全局模型，请确保其插件配置中的模型提供商项保持留空。",
  ])


def _extract_command_target(target: Any, message_str: Optional[str]) -> str:
  """安全解析指令目标参数（防御数字类型推断与空格截断）。"""
  clean_target = str(target).strip() if target is not None else ""
  if not message_str:
    return clean_target
  raw_msg = message_str.strip()
  for cmd_prefix in ("/gmsw", "gmsw"):
    if cmd_prefix in raw_msg:
      idx = raw_msg.find(cmd_prefix)
      tail = raw_msg[idx + len(cmd_prefix) :].strip()
      if tail:
        return tail
  return clean_target


async def _try_call_save_method(obj: Any, method_names: Tuple[str, ...]) -> bool:
  """尝试调用目标对象上的保存方法（支持同步和异步协程）。"""
  if obj is None:
    return False
  for name in method_names:
    func = getattr(obj, name, None)
    if not callable(func):
      continue
    if asyncio.iscoroutinefunction(func):
      await func()
    else:
      await asyncio.to_thread(func)
    return True
  return False


def _resolve_config_file_path(cfg: Any, context: Context) -> Optional[str]:
  """定位 AstrBot 配置文件物理路径。"""
  direct_path = (
      getattr(cfg, "config_path", None)
      or getattr(cfg, "_path", None)
      or getattr(context, "config_path", None)
  )
  if direct_path:
    return str(direct_path)

  candidates = (
      "data/cmd_config.json",
      "cmd_config.json",
      "data/config.json",
  )
  for candidate in candidates:
    if os.path.exists(candidate):
      return candidate
  return None


def _atomic_disk_write(path: str, data: Any) -> None:
  """通过临时文件原子替换持久化配置至磁盘。"""
  temp_path = f"{path}.tmp.{os.getpid()}"
  try:
    with open(temp_path, "w", encoding="utf-8") as f:
      json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, str(path))
  finally:
    if os.path.exists(temp_path):
      try:
        os.remove(temp_path)
      except OSError:
        pass


@register(
    "astrbot_plugin_global_model_switcher",
    "xixi",
    "AstrBot 全局 LLM 动态热切换插件",
    "1.0.2",
    "https://github.com/xixil/astrbot_plugin_global_model_switcher",
)
class GlobalModelSwitcherPlugin(Star):
  """AstrBot 全局默认 LLM 动态热切换插件。"""

  def __init__(
      self,
      context: Context,
      config: Optional[Dict[str, Any]] = None,
      *args: Any,
      **kwargs: Any,
  ):
    super().__init__(context)
    self.context: Context = context
    self.plugin_config: Dict[str, Any] = config or {}
    self._lock: asyncio.Lock = asyncio.Lock()
    logger.info("[GlobalModelSwitcher] 全局模型切换插件已成功加载")

  def _get_config_from_managers(self) -> Optional[Any]:
    """从配置管理器中探测全局配置。"""
    for mgr_attr in ("astrbot_config_mgr", "config_manager"):
      mgr = getattr(self.context, mgr_attr, None)
      if not mgr:
        continue
      for method_name in ("get_conf", "get_config"):
        fn = getattr(mgr, method_name, None)
        if not callable(fn):
          continue
        try:
          cfg = fn()
          if cfg is not None:
            return cfg
        except Exception:
          pass
    return None

  def _get_global_config(self) -> Optional[Any]:
    """安全获取 AstrBot 核心全局配置对象（兼容不同版本 Context 与管理器实现）。"""
    get_cfg = getattr(self.context, "get_config", None)
    if callable(get_cfg):
      try:
        cfg = get_cfg()
        if cfg is not None:
          return cfg
      except Exception:
        pass

    for attr in ("_config", "config"):
      cfg = getattr(self.context, attr, None)
      if cfg is not None:
        return cfg

    mgr_cfg = self._get_config_from_managers()
    if mgr_cfg is not None:
      return mgr_cfg

    pm = getattr(self.context, "provider_manager", None)
    settings = getattr(pm, "provider_settings", None)
    if isinstance(settings, dict):
      return {"provider_settings": settings}

    return None

  def _is_chat_provider(self, provider_type: str) -> bool:
    """判定 Provider 是否为可用于对话生成的文本模型服务。"""
    normalized_type = provider_type.strip().lower()
    return not any(
        non_chat in normalized_type for non_chat in NON_CHAT_PROVIDER_TYPES
    )

  def _parse_single_config_provider(
      self, item: Any, seen_ids: Set[str], disabled_ids: Set[str]
  ) -> Optional[Dict[str, Any]]:
    """解析单个配置中的 Provider 条目。"""
    if not isinstance(item, dict):
      return None
    p_id = (item.get("id") or item.get("provider_id") or "").strip()
    if not p_id:
      return None
    if not item.get("enable", True):
      disabled_ids.add(p_id)
      return None
    p_type = str(item.get("provider_type") or item.get("type") or "unknown")
    if not self._is_chat_provider(p_type):
      return None
    seen_ids.add(p_id)
    models = _extract_models(item.get("models"), item.get("model"))
    if not models and "/" in p_id:
      models = [p_id.split("/", 1)[1]]
    return {
        "id": p_id,
        "type": p_type,
        "models": models,
        "enable": True,
    }

  def _parse_config_providers(
      self, seen_ids: Set[str], disabled_ids: Set[str]
  ) -> List[Dict[str, Any]]:
    """从全局配置中提取所有启用的对话模型 Provider。"""
    cfg = self._get_global_config()
    if cfg is None or not hasattr(cfg, "get"):
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
    """安全获取运行时 Provider 实例列表，规避类型分配错误。"""
    get_all = getattr(self.context, "get_all_providers", None)
    if callable(get_all):
      try:
        res = get_all()
        if isinstance(res, list):
          return res
        if res:
          return list(res)
      except Exception:
        pass

    pm = getattr(self.context, "provider_manager", None)
    inst_map = getattr(pm, "inst_map", None)
    if isinstance(inst_map, dict):
      return list(inst_map.values())

    return []

  def _parse_single_runtime_provider(
      self, p: Any, seen_ids: Set[str], disabled_ids: Set[str]
  ) -> Optional[Dict[str, Any]]:
    """解析单个运行时 Provider 实例。"""
    meta = p.meta() if hasattr(p, "meta") and callable(p.meta) else None
    meta_dict = meta if isinstance(meta, dict) else {}
    p_cfg = getattr(p, "provider_config", None) or {}

    p_id = (
        meta_dict.get("id")
        or getattr(meta, "id", None)
        or getattr(p, "provider_id", None)
        or getattr(p, "id", None)
        or p_cfg.get("id")
        or p_cfg.get("provider_id")
    )
    if not p_id:
      return None

    clean_id = str(p_id).strip()
    is_enabled = getattr(p, "enable", None)
    if is_enabled is None:
      is_enabled = p_cfg.get("enable", True)

    if clean_id in seen_ids or clean_id in disabled_ids or not is_enabled:
      return None

    p_type = str(
        meta_dict.get("type")
        or getattr(meta, "type", None)
        or getattr(p, "provider_type", None)
        or getattr(p, "type", None)
        or p_cfg.get("provider_type")
        or p_cfg.get("type")
        or "chat_completion"
    )
    if not self._is_chat_provider(p_type):
      return None

    seen_ids.add(clean_id)
    models = _extract_models(
        getattr(p, "models", None) or p_cfg.get("models"),
        getattr(p, "model", None) or p_cfg.get("model"),
    )
    if not models and "/" in clean_id:
      models = [clean_id.split("/", 1)[1]]

    return {
        "id": clean_id,
        "type": p_type,
        "models": models,
        "enable": True,
    }

  def _parse_runtime_providers(
      self, seen_ids: Set[str], disabled_ids: Set[str]
  ) -> List[Dict[str, Any]]:
    """从运行时 ProviderManager 补充未收录的已启用对话模型实例。"""
    instances = self._get_runtime_instances()
    result: List[Dict[str, Any]] = []
    for p in instances:
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
      logger.warning(f"[GlobalModelSwitcher] 读取配置 provider 列表异常: {e}")
    try:
      providers.extend(self._parse_runtime_providers(seen_ids, disabled_ids))
    except Exception as e:
      logger.debug(f"[GlobalModelSwitcher] 读取运行时 provider 列表提示: {e}")
    return providers

  def get_current_default_provider_id(self) -> str:
    """获取当前全局生效的 default_provider_id，未设置时回退至首项。"""
    try:
      cfg = self._get_global_config()
      provider_settings = self._get_active_provider_settings(cfg)
      if isinstance(provider_settings, dict):
        default_id = provider_settings.get("default_provider_id")
        if default_id and isinstance(default_id, str) and default_id.strip():
          return default_id.strip()
    except Exception as e:
      logger.warning(f"[GlobalModelSwitcher] 读取 default_provider_id 异常: {e}")

    enabled = self.get_enabled_providers()
    return enabled[0]["id"] if enabled else ""

  def format_provider_list(
      self, providers: List[Dict[str, Any]], current_default_id: str
  ) -> str:
    """格式化 Provider 列表为可读文本消息。"""
    if not providers:
      return (
          "⚠️ 当前未检测到任何已启用的对话型 LLM Provider！\n"
          "💡 请先在 AstrBot WebUI「服务提供商」中配置并启用至少一个模型服务。"
      )

    lines: List[str] = [
        "🤖 【AstrBot 全局可用 LLM Provider 列表】",
        "------------------------------------",
    ]
    for idx, p in enumerate(providers, start=1):
      p_id = p["id"]
      models = p.get("models", [])
      models_str = " / ".join(models) if models else "默认模型"
      is_current = p_id == current_default_id
      prefix = "* " if is_current else "  "
      status_tag = " (当前全局默认)" if is_current else ""
      lines.extend([
          f"{prefix}[{idx}] {p_id}{status_tag}",
          f"      ├─ 模型: {models_str}",
          f"      └─ 类型: {p.get('type', '未知')}",
      ])

    lines.extend([
        "------------------------------------",
        f"📌 当前全局生效 ID: {current_default_id or '未显式指定'}",
        "💡 快捷切换方式: /gmsw <序号或ID>",
        "   例如: /gmsw 1 或 /gmsw deepseek_chat",
    ])
    return "\n".join(lines)

  async def _save_config_async(self, config: Optional[Any] = None) -> None:
    """异步持久化保存全局配置，避免阻塞主事件循环。"""
    cfg = config or self._get_global_config()

    if await _try_call_save_method(cfg, ("save_config_async", "save_config", "save")):
      return

    if await _try_call_save_method(self.context, ("save_config_async", "save_config")):
      return

    for mgr_name in ("astrbot_config_mgr", "config_manager"):
      mgr = getattr(self.context, mgr_name, None)
      if await _try_call_save_method(mgr, ("save_config", "save_conf", "save")):
        return

    if cfg is None:
      raise RuntimeError("无法获取 AstrBot 配置对象，无法执行保存")

    path = _resolve_config_file_path(cfg, self.context)
    if not path:
      raise RuntimeError("无法定位 AstrBot 配置文件路径，无法保存配置")

    await asyncio.to_thread(_atomic_disk_write, path, cfg)

  def _get_active_provider_settings(self, cfg: Any) -> Optional[Dict[str, Any]]:
    """安全获取当前生效的 provider_settings 字典。"""
    if isinstance(cfg, dict) and isinstance(cfg.get("provider_settings"), dict):
      return cfg["provider_settings"]
    pm = getattr(self.context, "provider_manager", None)
    pm_settings = getattr(pm, "provider_settings", None)
    if isinstance(pm_settings, dict):
      return pm_settings
    return None

  def _apply_default_provider_change(self, cfg: Any, target_id: str) -> None:
    """在全局配置和 ProviderManager 中同步应用新默认模型 ID。"""
    has_updated = False
    if isinstance(cfg, dict):
      if not isinstance(cfg.get("provider_settings"), dict):
        cfg["provider_settings"] = {}
      cfg["provider_settings"]["default_provider_id"] = target_id
      has_updated = True

    pm = getattr(self.context, "provider_manager", None)
    pm_settings = getattr(pm, "provider_settings", None)
    if isinstance(pm_settings, dict):
      pm_settings["default_provider_id"] = target_id
      has_updated = True

    if not has_updated:
      raise RuntimeError("未能定位到全局配置对象或 ProviderManager，无法修改配置")

  async def _rollback_provider_settings(
      self, snapshot: Optional[Dict[str, Any]]
  ) -> None:
    """异常时安全还原 provider_settings 配置。"""
    cfg = self._get_global_config()
    if cfg is not None and isinstance(cfg, dict):
      if snapshot is None:
        cfg.pop("provider_settings", None)
      else:
        cfg["provider_settings"] = snapshot

    pm = getattr(self.context, "provider_manager", None)
    pm_settings = getattr(pm, "provider_settings", None)
    if isinstance(pm_settings, dict) and snapshot is not None and "default_provider_id" in snapshot:
      pm_settings["default_provider_id"] = snapshot["default_provider_id"]

    try:
      await self._save_config_async(cfg)
    except Exception as rb_err:
      logger.critical(
          f"[GlobalModelSwitcher] 配置回滚保存失败: {rb_err}", exc_info=True
      )

  @filter.permission_type(filter.PermissionType.ADMIN)
  @filter.command("gmls")
  async def gmls(self, event: AstrMessageEvent):
    """列出当前所有已启用的 LLM Provider 及其模型信息，并标记全局默认项。"""
    providers = self.get_enabled_providers()
    current_default = self.get_current_default_provider_id()
    yield event.plain_result(
        self.format_provider_list(providers, current_default)
    )

  @filter.permission_type(filter.PermissionType.ADMIN)
  @filter.command("gmsw")
  async def gmsw(self, event: AstrMessageEvent, target: Optional[Any] = None):
    """热切换 AstrBot 全局默认 LLM Provider（支持指定序号或 Provider ID）。"""
    clean_target = _extract_command_target(target, getattr(event, "message_str", None))
    if not clean_target:
      yield event.plain_result(
          "⚠️ 请指定目标 LLM Provider 的序号或 ID。\n"
          "📌 用法: /gmsw <序号或ID>\n"
          "👉 示例: /gmsw 1 或 /gmsw deepseek_chat\n"
          "💡 可发送 /gmls 查看可用列表。"
      )
      return

    providers = self.get_enabled_providers()
    if not providers:
      yield event.plain_result("⚠️ 未检索到任何已启用的对话型 LLM Provider，无法切换。")
      return

    target_p, err_msg = _find_target_provider(clean_target, providers)
    if err_msg or not target_p:
      yield event.plain_result(err_msg or "⚠️ 未找到目标 Provider。")
      return

    target_id = target_p["id"]
    async with self._lock:
      cfg = self._get_global_config()
      settings = self._get_active_provider_settings(cfg)
      old_default = (
          settings.get("default_provider_id", "").strip()
          if isinstance(settings, dict)
          else ""
      )
      current_default = self.get_current_default_provider_id()

      if target_id == current_default and old_default == target_id:
        yield event.plain_result(
            f"ℹ️ 当前全局默认 Provider 已经是「{target_id}」，无需重复切换。"
        )
        return

      snapshot = copy.deepcopy(settings) if isinstance(settings, dict) else None
      try:
        self._apply_default_provider_change(cfg, target_id)
        await self._save_config_async(cfg)
        yield event.plain_result(_build_success_message(target_p, old_default))
      except Exception as e:
        logger.error(
            f"[GlobalModelSwitcher] 切换全局 Provider 异常，执行回滚: {e}",
            exc_info=True,
        )
        await self._rollback_provider_settings(snapshot)
        yield event.plain_result(
            f"❌ 切换全局 Provider 失败！\n🚨 原因: {e}\n🛡️ 已安全回滚至修改前状态。"
        )

  async def terminate(self):
    """插件卸载清理逻辑。"""
    logger.info("[GlobalModelSwitcher] 全局模型切换插件已卸载")
