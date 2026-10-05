r"""插件系统管理器与内置/外部插件支持。

所有规则统一作为插件（内置插件与外部插件）运行：
- 内置插件：关键词引擎、正则引擎、共同好友引擎（支持 {count} 占位符）、LLM引擎、排除过滤。
- 外部插件：存储于用户数据目录 (%LOCALAPPDATA%\VRCProfileRadar\plugins) 或开发目录 (plugins_dev)。
- 零重复拉取：插件通过上下文获取用户资料与共同好友关系，优先使用系统已有的本地与 SQLite 缓存。
"""
import importlib.util
import json
import os
import re
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import rules as rules_mod
from rules import (
    Rule, KEYWORD, REGEX, LLM, EXCLUDE, MUTUAL,
    compare_count, format_rule_display_name, fields_text
)


@dataclass
class PluginUserInfo:
    """包装传递给插件的用户信息，方便属性访问与缓存查询。"""
    user_id: str = ""
    name: str = ""
    displayName: str = ""
    bio: str = ""
    bioLinks: list = field(default_factory=list)
    bio_links: list = field(default_factory=list)
    status: str = ""
    statusDescription: str = ""
    status_description: str = ""
    pronouns: str = ""
    tags: list = field(default_factory=list)
    avatar: str = ""
    date_joined: str = ""
    raw_profile: dict = field(default_factory=dict)
    _context: Any = None

    @classmethod
    def from_profile(cls, user_id: str, profile: dict, context=None):
        name = profile.get("displayName") or ""
        links = profile.get("bioLinks") or []
        bio = profile.get("bio") or ""
        status = profile.get("status") or ""
        status_desc = profile.get("statusDescription") or ""
        return cls(
            user_id=user_id,
            name=name,
            displayName=name,
            bio=bio,
            bioLinks=links,
            bio_links=links,
            status=status,
            statusDescription=status_desc,
            status_description=status_desc,
            pronouns=profile.get("pronouns") or "",
            tags=profile.get("tags") or [],
            avatar=profile.get("avatar") or "",
            date_joined=profile.get("date_joined") or "",
            raw_profile=profile,
            _context=context,
        )

    def get_mutual_count(self) -> Optional[int]:
        """获取与登录账号的共同好友数量（优先读本地缓存，无多余网络请求）。"""
        if self._context and hasattr(self._context, "get_mutual_count"):
            return self._context.get_mutual_count(self.user_id)
        return None

    def get_mutual_friends(self) -> Optional[list]:
        """获取与登录账号的共同好友列表（优先读本地缓存，无多余网络请求）。"""
        if self._context and hasattr(self._context, "get_mutual_friends"):
            return self._context.get_mutual_friends(self.user_id)
        return None


class BasePlugin:
    """插件基类。"""
    id: str = ""
    name: str = ""
    version: str = "1.0.0"
    author: str = ""
    description: str = ""
    is_builtin: bool = False
    source: str = "custom"  # "builtin" | "dev" | "user"
    path: str = ""
    enabled: bool = True
    error: str = ""

    def __init__(self, context=None):
        self.context = context

    def get_rule_types(self) -> List[dict]:
        """返回插件提供的规则类型定义列表。"""
        return []

    def match_rule(self, context, user_info: PluginUserInfo, rule: dict) -> Optional[Any]:
        """对具体规则执行匹配，默认回退调用 match。"""
        return self.match(context, user_info)

    def match(self, context, user_info: PluginUserInfo) -> Optional[Any]:
        """对传入用户进行匹配。"""
        return None

    def handle_action(self, action: dict, user_info: PluginUserInfo, match_entry: dict):
        """当插件返回自定义 action 时触发。"""
        pass


# =====================================================================
# 内置插件：将所有基础规则体系作为内置插件驱动
# =====================================================================

class BuiltinKeywordPlugin(BasePlugin):
    id = "builtin_keyword"
    name = "关键词规则引擎"
    version = "1.0.0"
    author = "System"
    description = "内置插件：支持多字段关键词匹配（任意命中 / 全部命中）。"
    is_builtin = True
    source = "builtin"

    def match(self, context, user_info: PluginUserInfo):
        if not context or not context.matcher:
            return None
        rules = context.matcher._enabled_rules(KEYWORD)
        results = []
        profile = user_info.raw_profile
        for rule in rules:
            hits = rules_mod.match_keyword(rule, profile)
            if hits:
                results.append({
                    "id": rule.id,
                    "rule_id": rule.id,
                    "plugin_id": self.id,
                    "tag_text": rule.name,
                    "tag_color": rule.color,
                    "reason": "、".join(hits[:5]),
                    "type": KEYWORD,
                    "action": {"type": "detail"},
                })
        return results if results else None


class BuiltinRegexPlugin(BasePlugin):
    id = "builtin_regex"
    name = "正则表达式引擎"
    version = "1.0.0"
    author = "System"
    description = "内置插件：支持基于正则表达式匹配个人资料与外部链接。"
    is_builtin = True
    source = "builtin"

    def match(self, context, user_info: PluginUserInfo):
        if not context or not context.matcher:
            return None
        rules = context.matcher._enabled_rules(REGEX)
        results = []
        profile = user_info.raw_profile
        for rule in rules:
            hits = rules_mod.match_regex(rule, profile)
            if hits:
                results.append({
                    "id": rule.id,
                    "rule_id": rule.id,
                    "plugin_id": self.id,
                    "tag_text": rule.name,
                    "tag_color": rule.color,
                    "reason": "、".join(hits[:5]),
                    "type": REGEX,
                    "action": {"type": "detail"},
                })
        return results if results else None


class BuiltinMutualFriendsPlugin(BasePlugin):
    id = "builtin_mutual_friends"
    name = "共同好友分析引擎"
    version = "1.0.0"
    author = "System"
    description = "内置插件：分析与当前玩家的共同好友关系；名称支持 {count} 占位符动态替换实际数量。"
    is_builtin = True
    source = "builtin"

    def match_user_rule(self, context, rule, uid, player_name, profile):
        """单独执行一条共同好友规则检测，返回格式化好的匹配字典（含 {count} 替换）。"""
        me = context.get_me_uid() if context else ""
        if not me:
            return None
        limit_users = getattr(rule, "limit_users", False)
        if limit_users:
            friends = context.get_mutual_friends(uid) if context else None
            if friends is None:
                return None
            target_uids = {u["user_id"] for u in (rule.users or []) if u.get("user_id")}
            matched_count = sum(1 for f in friends if (f.get("id") or f.get("userId")) in target_uids)
            hit = compare_count(rule.operator, matched_count, rule.count)
            actual_count = matched_count
            reason = "指定共同好友 %d 人" % matched_count
        else:
            count = context.get_mutual_count(uid) if context else None
            if count is None:
                return None
            hit = compare_count(rule.operator, count, rule.count)
            actual_count = count
            reason = "共同好友 %d 人" % count

        if not hit:
            return None

        # 支持 {count} 占位符实时替换
        display_name = format_rule_display_name(rule.name, actual_count)

        return {
            "id": rule.id,
            "rule_id": rule.id,
            "plugin_id": self.id,
            "tag_text": display_name,
            "tag_color": rule.color,
            "reason": reason,
            "type": MUTUAL,
            "actual_count": actual_count,
            "action": {"type": "detail"},
        }


class BuiltinLLMPlugin(BasePlugin):
    id = "builtin_llm"
    name = "AI智能判断引擎"
    version = "1.0.0"
    author = "System"
    description = "内置插件：使用大语言模型进行高语义维度的资料理解与匹配。"
    is_builtin = True
    source = "builtin"


class BuiltinExcludePlugin(BasePlugin):
    id = "builtin_exclude"
    name = "排除与黑名单过滤"
    version = "1.0.0"
    author = "System"
    description = "内置插件：排除自身、登录账号好友及指定名单用户。"
    is_builtin = True
    source = "builtin"


# =====================================================================
# 外部插件（目录格式：plugin.json + plugin.py）
# =====================================================================

class DirectoryPlugin(BasePlugin):
    """从文件夹加载的外部插件。"""
    def __init__(self, folder_path: str, source: str = "custom", context=None):
        super().__init__(context=context)
        self.path = folder_path
        self.source = source
        self.module = None
        self.instance = None
        self._load()

    def _load(self):
        json_path = os.path.join(self.path, "plugin.json")
        manifest_path = os.path.join(self.path, "manifest.json")
        manifest = {}
        target_json = json_path if os.path.exists(json_path) else (manifest_path if os.path.exists(manifest_path) else None)
        if target_json:
            try:
                with open(target_json, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
            except Exception as e:
                self.error = f"读取配置失败: {e}"

        self.id = manifest.get("id") or os.path.basename(self.path)
        self.name = manifest.get("name") or self.id
        self.version = manifest.get("version") or "1.0.0"
        self.author = manifest.get("author") or ""
        self.description = manifest.get("description") or ""
        self.manifest = manifest
        self.rule_types = manifest.get("rule_types") or ([manifest["rule_type"]] if manifest.get("rule_type") else [])
        if not self.rule_types:
            self.rule_types = [{
                "type": self.id,
                "name": self.name,
                "plugin_id": self.id,
                "description": self.description,
                "default_name": self.name,
                "default_color": "#ffd700",
                "fields": [],
            }]

        py_path = os.path.join(self.path, "plugin.py")
        if not os.path.exists(py_path):
            self.error = "缺少 plugin.py 入口文件"
            return

        module_name = f"vrc_radar_plugin_{self.id}_{abs(hash(self.path))}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, py_path)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = mod
                spec.loader.exec_module(mod)
                self.module = mod

                # 寻找 Plugin 类或导出函数
                if hasattr(mod, "Plugin") and callable(mod.Plugin):
                    try:
                        self.instance = mod.Plugin(self.context)
                    except TypeError:
                        self.instance = mod.Plugin()
                else:
                    self.instance = mod
                self.error = ""
            else:
                self.error = "无法构建模块规格"
        except Exception as e:
            self.error = f"加载脚本出错: {e}"

    def get_rule_types(self) -> List[dict]:
        if hasattr(self.instance, "get_rule_types") and callable(self.instance.get_rule_types):
            try:
                custom_types = self.instance.get_rule_types()
                if isinstance(custom_types, list) and custom_types:
                    return custom_types
            except Exception:
                pass
        return self.rule_types or []

    def match_rule(self, context, user_info: PluginUserInfo, rule: dict) -> Optional[Any]:
        if self.error or not self.instance:
            return None
        fn = getattr(self.instance, "match_rule", None) or getattr(self.instance, "match", None)
        if not callable(fn):
            return None
        try:
            import inspect
            sig = inspect.signature(fn)
            cnt = len(sig.parameters)
            if cnt >= 3:
                return fn(context, user_info, rule)
            elif cnt == 2:
                param_names = list(sig.parameters.keys())
                if "rule" in param_names:
                    return fn(user_info, rule)
                try:
                    return fn(context, user_info)
                except TypeError:
                    return fn(user_info, rule)
            else:
                return fn(user_info)
        except Exception as exc:
            print(f"[插件错误] {self.name} 运行 match_rule 失败: {exc}")
            return None

    def match(self, context, user_info: PluginUserInfo):
        return self.match_rule(context, user_info, {})

    def handle_action(self, action: dict, user_info: PluginUserInfo, match_entry: dict):
        if self.error or not self.instance:
            return
        fn = getattr(self.instance, "handle_action", None) or getattr(self.instance, "on_action", None)
        if callable(fn):
            try:
                fn(action, user_info, match_entry)
            except Exception as exc:
                print(f"[插件错误] {self.name} 执行动作失败: {exc}")


# =====================================================================
# 插件管理器
# =====================================================================

class PluginManager:
    """插件管理器：发现、加载、启停控制并向匹配流水线提供统一服务。"""

    def __init__(self, config, cache=None, api=None, matcher=None, project_dir=None):
        self.config = config
        self.cache = cache
        self.api = api
        self.matcher = matcher
        self.project_dir = project_dir or os.path.dirname(os.path.abspath(__file__))
        self.dev_plugins_dir = os.path.join(self.project_dir, "plugins_dev")
        self.user_plugins_dir = os.path.join(self.config.data_dir, "plugins")
        self._lock = threading.RLock()
        self.plugins: Dict[str, BasePlugin] = {}

        self._init_dirs()
        self.reload()

    def _init_dirs(self):
        try:
            os.makedirs(self.dev_plugins_dir, exist_ok=True)
            os.makedirs(self.user_plugins_dir, exist_ok=True)
        except OSError:
            pass

    def get_me_uid(self) -> str:
        if self.matcher and hasattr(self.matcher, "_get_me_uid"):
            return self.matcher._get_me_uid()
        return ""

    def get_mutual_count(self, uid: str) -> Optional[int]:
        if self.matcher and hasattr(self.matcher, "_get_mutual_count"):
            me = self.get_me_uid()
            if me:
                return self.matcher._get_mutual_count(me, uid)
        return None

    def get_mutual_friends(self, uid: str) -> Optional[list]:
        if self.matcher and hasattr(self.matcher, "_get_mutual_friends_list"):
            me = self.get_me_uid()
            if me:
                return self.matcher._get_mutual_friends_list(me, uid)
        return None

    def is_disabled(self, plugin_id: str) -> bool:
        disabled = self.config.get("plugins", "disabled", default=[]) or []
        return plugin_id in disabled

    def set_plugin_enabled(self, plugin_id: str, enabled: bool):
        with self._lock:
            disabled = set(self.config.get("plugins", "disabled", default=[]) or [])
            if enabled:
                disabled.discard(plugin_id)
            else:
                disabled.add(plugin_id)
            self.config.set(list(disabled), "plugins", "disabled")
            if plugin_id in self.plugins:
                self.plugins[plugin_id].enabled = enabled

    def reload(self):
        """重新扫描并加载内置插件与外部插件。"""
        with self._lock:
            new_plugins = {}

            # 1. 注册内置插件
            builtins = [
                BuiltinKeywordPlugin(self),
                BuiltinRegexPlugin(self),
                BuiltinMutualFriendsPlugin(self),
                BuiltinLLMPlugin(self),
                BuiltinExcludePlugin(self),
            ]
            for p in builtins:
                p.enabled = not self.is_disabled(p.id)
                new_plugins[p.id] = p

            # 2. 扫描开发目录 (plugins_dev)
            if os.path.isdir(self.dev_plugins_dir):
                for item in sorted(os.listdir(self.dev_plugins_dir)):
                    full_p = os.path.join(self.dev_plugins_dir, item)
                    if os.path.isdir(full_p):
                        p = DirectoryPlugin(full_p, source="dev", context=self)
                        p.enabled = not self.is_disabled(p.id)
                        new_plugins[p.id] = p

            # 3. 扫描用户数据目录 (AppData/../plugins)
            if os.path.isdir(self.user_plugins_dir):
                for item in sorted(os.listdir(self.user_plugins_dir)):
                    full_p = os.path.join(self.user_plugins_dir, item)
                    if os.path.isdir(full_p):
                        p = DirectoryPlugin(full_p, source="user", context=self)
                        p.enabled = not self.is_disabled(p.id)
                        new_plugins[p.id] = p

            self.plugins = new_plugins

    def get_all_plugins(self) -> List[BasePlugin]:
        with self._lock:
            return list(self.plugins.values())

    def get_available_rule_types(self) -> List[dict]:
        """获取所有当前已启用的插件所提供的规则类型列表。"""
        with self._lock:
            out = []
            for p in self.plugins.values():
                if not p.is_builtin and p.enabled and not p.error:
                    for rt in p.get_rule_types():
                        rt_copy = dict(rt)
                        rt_copy["plugin_id"] = p.id
                        out.append(rt_copy)
            return out

    def get_rule_type(self, type_name: str) -> Optional[dict]:
        """按规则类型名称或标识获取对应定义。"""
        for rt in self.get_available_rule_types():
            if rt.get("type") == type_name:
                return rt
        return None

    def match_external_plugins(self, user_info: PluginUserInfo, enabled_rules: Optional[List[Any]] = None) -> List[dict]:
        """对传入用户运行在规则配置中添加并启用的插件规则。"""
        if enabled_rules is None:
            if not self.matcher or not hasattr(self.matcher, "_enabled_rules"):
                return []
            enabled_rules = self.matcher._enabled_rules()

        available_types = {rt["type"]: rt for rt in self.get_available_rule_types()}
        if not available_types:
            return []

        results = []
        for r in enabled_rules:
            r_dict = r.to_dict() if hasattr(r, "to_dict") else dict(r)
            rtype = r_dict.get("type")
            if rtype not in available_types:
                continue

            rt_def = available_types[rtype]
            plugin_id = rt_def.get("plugin_id")
            plugin = self.plugins.get(plugin_id)
            if not plugin or not plugin.enabled or plugin.error:
                continue

            try:
                res = plugin.match_rule(self, user_info, r_dict)
            except Exception as e:
                print(f"[插件异常] {plugin.name} 规则「{r_dict.get('name')}」匹配失败: {e}")
                continue

            if not res:
                continue

            items = res if isinstance(res, list) else [res]
            for it in items:
                if not isinstance(it, dict):
                    continue
                tag_text = it.get("tag_text") or it.get("name") or r_dict.get("name") or plugin.name
                tag_color = it.get("tag_color") or it.get("color") or r_dict.get("color") or "#ffd700"
                reason = it.get("reason") or ""
                action = it.get("action")
                if not action:
                    action = {"type": "detail"}

                entry = {
                    "id": r_dict.get("id") or f"plugin:{plugin.id}",
                    "name": tag_text,
                    "color": tag_color,
                    "reason": reason,
                    "type": "plugin",
                    "plugin_rule_type": rtype,
                    "plugin_id": plugin.id,
                    "action": action,
                }
                results.append(entry)
        return results

    def handle_chip_action(self, rule: dict, match: dict, on_show_detail: Optional[Callable] = None):
        """统一分发标签点击动作。"""
        action = rule.get("action") or {}
        atype = action.get("type", "detail")

        if atype == "open_url":
            url = action.get("url")
            if url:
                try:
                    webbrowser.open(url)
                    return
                except Exception as exc:
                    print(f"打开网页失败: {exc}")
        elif atype == "detail":
            if on_show_detail:
                on_show_detail(match)
            return

        # 若插件自定义了其它动作回调
        plugin_id = rule.get("plugin_id")
        if plugin_id and plugin_id in self.plugins:
            p = self.plugins[plugin_id]
            uinfo = PluginUserInfo.from_profile(
                match.get("user_id", ""),
                match.get("profile") or {},
                context=self
            )
            p.handle_action(action, uinfo, match)
            return

        # 默认回退打开用户详情
        if on_show_detail:
            on_show_detail(match)
