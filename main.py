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


def _extract_models(models_raw: Any, fallback_model: Any = None) -> List[str]:
  """提取配置或运行时实例中启用的模型名称列表。"""
  seen: Set[str] = set()
  result: List[str] = []

  def _append_model(name: Any) -> None:
    if name is None:
      return
    cleaned = str(name).strip()
    if cleaned and cleaned not in seen:
      seen.add(cleaned)
      result.append(cleaned)

  if isinstance(models_raw, list):
    for item in models_raw:
      if isinstance(item, dict) and item.get("enable", True):
        m_id = (
            item.get("model_id")
            or item.get("name")
            or item.get("id")
            or item.get("model")
        )
        _append_model(m_id)
      elif isinstance(item, str):
        _append_model(item)
  elif isinstance(models_raw, str):
    _append_model(models_raw)

  if not result and fallback_model is not None:
    if isinstance(fallback_model, (list, dict)):
      return _extract_models(fallback_model)
    _append_model(fallback_model)

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

  def _get_global_config(self) -> Optional[Any]:
    """安全获取 AstrBot 核心全局配置对象（兼容不同版本 Context 与管理器实现）。"""
    if hasattr(self.context, "get_config") and callable(self.context.get_config):
      try:
        cfg = self.context.get_config()
        if cfg is not None:
          return cfg
      except Exception:
        pass

    for attr in ("_config", "config"):
      cfg = getattr(self.context, attr, None)
      if cfg is not None:
        return cfg

    for mgr_attr in ("astrbot_config_mgr", "config_manager"):
      mgr = getattr(self.context, mgr_attr, None)
      if mgr:
        for method_name in ("get_conf", "get_config"):
          fn = getattr(mgr, method_name, None)
          if callable(fn):
            try:
              cfg = fn()
              if cfg is not None:
                return cfg
            except Exception:
              pass

    pm = getattr(self.context, "provider_manager", None)
    if pm and hasattr(pm, "provider_settings") and isinstance(pm.provider_settings, dict):
      return {"provider_settings": pm.provider_settings}

    return None

  def _is_chat_provider(self, provider_type: str) -> bool:
    """判定 Provider 是否为可用于对话生成的文本模型服务。"""
    normalized_type = provider_type.strip().lower()
    return not any(
        non_chat in normalized_type for non_chat in NON_CHAT_PROVIDER_TYPES
    )

  def _parse_config_providers(
      self, seen_ids: Set[str], disabled_ids: Set[str]
  ) -> List[Dict[str, Any]]:
    """从全局配置中提取所有启用的对话模型 Provider。"""
    cfg = self._get_global_config()
    if cfg is None or not hasattr(cfg, "get"):
      return []
    raw = cfg.get("provider") or cfg.get("providers") or []
    items = (
        raw
        if isinstance(raw, list)
        else (list(raw.values()) if isinstance(raw, dict) else [])
    )
    result: List[Dict[str, Any]] = []
    for item in items:
      if not isinstance(item, dict):
        continue
      p_id = (item.get("id") or item.get("provider_id") or "").strip()
      if not p_id:
        continue
      if not item.get("enable", True):
        disabled_ids.add(p_id)
        continue
      p_type = str(item.get("provider_type") or item.get("type") or "unknown")
      if not self._is_chat_provider(p_type):
        continue
      seen_ids.add(p_id)
      models = _extract_models(item.get("models"), item.get("model"))
      if not models and "/" in p_id:
        models = [p_id.split("/", 1)[1]]
      result.append({
          "id": p_id,
          "type": p_type,
          "models": models,
          "enable": True,
      })
    return result

  def _parse_runtime_providers(
      self, seen_ids: Set[str], disabled_ids: Set[str]
  ) -> List[Dict[str, Any]]:
    """从运行时 ProviderManager 补充未收录的已启用对话模型实例。"""
    instances: List[Any] = []
    if hasattr(self.context, "get_all_providers") and callable(
        self.context.get_all_providers
    ):
      instances = self.context.get_all_providers() or []
    elif hasattr(self.context, "provider_manager") and hasattr(
        self.context.provider_manager, "inst_map"
    ):
      instances = list(self.context.provider_manager.inst_map.values())

    result: List[Dict[str, Any]] = []
    for p in instances:
      meta = p.meta() if hasattr(p, "meta") and callable(p.meta) else None
      meta_id = meta.get("id") if isinstance(meta, dict) else getattr(meta, "id", None)
      p_cfg = getattr(p, "provider_config", None) or {}
      p_id = (
          meta_id
          or getattr(p, "provider_id", None)
          or getattr(p, "id", None)
          or p_cfg.get("id")
          or p_cfg.get("provider_id")
      )
      if not p_id:
        continue
      clean_id = str(p_id).strip()
      is_enabled = getattr(p, "enable", None)
      if is_enabled is None:
        is_enabled = p_cfg.get("enable", True)
      if (
          clean_id in seen_ids
          or clean_id in disabled_ids
          or not is_enabled
      ):
        continue
      meta_type = meta.get("type") if isinstance(meta, dict) else getattr(meta, "type", None)
      p_type = str(
          meta_type
          or getattr(p, "provider_type", None)
          or getattr(p, "type", None)
          or p_cfg.get("provider_type")
          or p_cfg.get("type")
          or "chat_completion"
      )
      if not self._is_chat_provider(p_type):
        continue
      seen_ids.add(clean_id)
      models = _extract_models(
          getattr(p, "models", None) or p_cfg.get("models"),
          getattr(p, "model", None) or p_cfg.get("model"),
      )
      if not models and "/" in clean_id:
        models = [clean_id.split("/", 1)[1]]
      result.append({
          "id": clean_id,
          "type": p_type,
          "models": models,
          "enable": True,
      })
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
          f"[GlobalModelSwitcher] 读取配置 provider 列表异常: {e}"
      )
    try:
      providers.extend(self._parse_runtime_providers(seen_ids, disabled_ids))
    except Exception as e:
      logger.debug(
          f"[GlobalModelSwitcher] 读取运行时 provider 列表提示: {e}"
      )
    return providers

  def get_current_default_provider_id(self) -> str:
    """获取当前全局生效的 default_provider_id，未设置时回退至首项。"""
    try:
      cfg = self._get_global_config()
      provider_settings = None
      if cfg is not None and hasattr(cfg, "get"):
        provider_settings = cfg.get("provider_settings")
      if not provider_settings:
        pm = getattr(self.context, "provider_manager", None)
        if pm and hasattr(pm, "provider_settings"):
          provider_settings = pm.provider_settings
      if isinstance(provider_settings, dict):
        default_id = provider_settings.get("default_provider_id")
        if default_id and isinstance(default_id, str) and default_id.strip():
          return default_id.strip()
    except Exception as e:
      logger.warning(
          f"[GlobalModelSwitcher] 读取 default_provider_id 异常: {e}"
      )

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
      lines.append(f"{prefix}[{idx}] {p_id}{status_tag}")
      lines.append(f"      ├─ 模型: {models_str}")
      lines.append(f"      └─ 类型: {p.get('type', '未知')}")

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

    if cfg is not None:
      for method_name in ("save_config_async", "save_config", "save"):
        func = getattr(cfg, method_name, None)
        if callable(func):
          if asyncio.iscoroutinefunction(func):
            await func()
          else:
            await asyncio.to_thread(func)
          return

    for ctx_method in ("save_config_async", "save_config"):
      func = getattr(self.context, ctx_method, None)
      if callable(func):
        if asyncio.iscoroutinefunction(func):
          await func()
        else:
          await asyncio.to_thread(func)
        return

    for mgr_attr in ("astrbot_config_mgr", "config_manager"):
      mgr = getattr(self.context, mgr_attr, None)
      if mgr:
        for mgr_save in ("save_config", "save_conf", "save"):
          func = getattr(mgr, mgr_save, None)
          if callable(func):
            if asyncio.iscoroutinefunction(func):
              await func()
            else:
              await asyncio.to_thread(func)
            return

    def _fallback_disk_write() -> None:
      if cfg is None:
        raise RuntimeError("无法获取 AstrBot 配置对象，无法执行保存")
      path = (
          getattr(cfg, "config_path", None)
          or getattr(cfg, "_path", None)
          or getattr(self.context, "config_path", None)
      )
      if not path:
        for candidate in (
            "data/cmd_config.json",
            "cmd_config.json",
            "data/config.json",
        ):
          if os.path.exists(candidate):
            path = candidate
            break
      if not path:
        raise RuntimeError("无法定位 AstrBot 配置文件路径，无法保存配置")
      temp_path = f"{path}.tmp.{os.getpid()}"
      try:
        with open(temp_path, "w", encoding="utf-8") as f:
          json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, str(path))
      finally:
        if os.path.exists(temp_path):
          try:
            os.remove(temp_path)
          except OSError:
            pass

    await asyncio.to_thread(_fallback_disk_write)

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
    if pm and hasattr(pm, "provider_settings") and isinstance(pm.provider_settings, dict):
      if snapshot is not None and "default_provider_id" in snapshot:
        pm.provider_settings["default_provider_id"] = snapshot["default_provider_id"]

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
    clean_target = str(target).strip() if target is not None else ""
    if event.message_str:
      raw_msg = event.message_str.strip()
      for cmd_prefix in ("/gmsw", "gmsw"):
        if cmd_prefix in raw_msg:
          idx = raw_msg.find(cmd_prefix)
          tail = raw_msg[idx + len(cmd_prefix) :].strip()
          if tail:
            clean_target = tail
            break
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
      settings = None
      if cfg is not None and hasattr(cfg, "get"):
        settings = cfg.get("provider_settings")
      if not settings:
        pm = getattr(self.context, "provider_manager", None)
        if pm and hasattr(pm, "provider_settings"):
          settings = pm.provider_settings

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
        if cfg is not None and isinstance(cfg, dict):
          if not isinstance(cfg.get("provider_settings"), dict):
            cfg["provider_settings"] = {}
          cfg["provider_settings"]["default_provider_id"] = target_id

        pm = getattr(self.context, "provider_manager", None)
        if pm and hasattr(pm, "provider_settings") and isinstance(pm.provider_settings, dict):
          pm.provider_settings["default_provider_id"] = target_id

        if cfg is None and (not pm or not hasattr(pm, "provider_settings")):
          raise RuntimeError("未能定位到全局配置对象或 ProviderManager，无法修改配置")

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
