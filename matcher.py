"""扫描流水线：日志事件 -> 限流拉取资料 -> 本地规则 + LLM 规则 -> 输出匹配结果。

线程模型：
  LogTailer 线程 -> handle_log_event()（只更新玩家表、安排扫描）
  扫描工作线程 -> run_scan()（拉资料、跑规则、emit 结果）
  头像线程池   -> 下载匹配用户的头像缩略图
结果通过 emit(event dict) 发给各 sink（UI 队列 / 控制台）。
"""
import hashlib
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import rules as rules_mod
from rules import (
    Rule, KEYWORD, REGEX, LLM, EXCLUDE, MUTUAL, compare_count, format_rule_display_name
)
from plugin_manager import PluginManager, PluginUserInfo
from llm_matcher import LLMMatcher, LLMNotConfigured
from vrchat_api import VRChatAPIError

FRIENDS_TTL = 900       # 好友列表刷新间隔（秒）
MUTUAL_TTL = 86400      # 共同好友数缓存时长（秒）


def parse_mutual_count(data):
    """从 /users/{id}/mutuals 响应中解析共同好友数量（隐藏的好友也计入）。

    实测响应形如 {"friends": N, "groups": M}；其余键名作为兼容兜底。
    """
    if not isinstance(data, dict):
        return None
    for key in ("friends", "friendCount", "friendsCount", "mutualFriendCount",
                "mutualFriends"):
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, list):
            return len(value)
    return None

PROFILE_FIELDS = (
    "displayName", "bio", "bioLinks", "pronouns", "tags", "trustTags",
    "status", "statusDescription", "date_joined", "last_platform",
    "iconUrl", "profilePicOverride", "userIcon",
    "currentAvatarThumbnailImageUrl", "currentAvatarImageUrl",
)
_AVATAR_FALLBACK = (
    "iconUrl", "profilePicOverride", "userIcon",
    "currentAvatarThumbnailImageUrl", "currentAvatarImageUrl",
)


def slim_profile(full):
    """从 API 原始响应提取匹配与展示所需的字段。

    头像按 fallback 链取（/profile 与 /users 现在只返回 iconUrl，旧字段保留兜底）；
    tags 缺失时用 trustTags（/profile 返回的信任等级）代替。
    """
    profile = {
        "displayName": full.get("displayName", ""),
        "bio": full.get("bio", ""),
        "bioLinks": full.get("bioLinks") or [],
        "avatar": "",
        "pronouns": full.get("pronouns", ""),
        "status": full.get("status", ""),
        "statusDescription": full.get("statusDescription", ""),
        "date_joined": full.get("date_joined", ""),
    }
    tags = full.get("tags") or full.get("trustTags")
    if tags:
        profile["tags"] = tags
    for key in _AVATAR_FALLBACK:
        if full.get(key):
            profile["avatar"] = full[key]
            break
    return profile


class Matcher:
    def __init__(self, config, cache, api=None, log_dir=""):
        self.config = config
        self.cache = cache
        self.api = api
        self.llm = LLMMatcher(config, cache)
        self.sinks = []
        self.debounce = float(config.get("scan", "debounce_seconds", default=3.0) or 3.0)
        self.profile_cache_hours = float(config.get("api", "profile_cache_hours", default=4) or 4)

        self._players = {}          # key -> {"name", "user_id"}
        self._profiles = {}         # user_id -> slim profile
        self._matches = {}          # user_id -> {"user_id","name","rules":[...]}
        self._failed_uids = set()   # 最近一次扫描里获取失败的玩家
        self._scanning = False
        self._fetching_uid = None
        self._fetching_uids = set()
        self.last_scan_time = 0.0
        self._local_uid = ""
        self._local_name = ""
        self.location = ""
        self.world_name = ""
        self._cookie_error = False
        self._llm_warned = False

        # 排除/共同好友相关状态
        self._friends = {}          # Cookie 账号好友：uid -> 名字
        self._friends_at = 0.0
        self._friends_loading = False
        self._me_uid = ""           # Cookie 账号的用户 id（共同好友数以它为基准）

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wakeup = threading.Event()
        self._timer = None
        self._scan_thread = None
        self._avatar_pool = None
        self._scan_pool = None
        self.avatar_dir = getattr(
            config, "avatar_dir", os.path.join(os.path.dirname(os.path.abspath(config.path)), "cache", "avatars")
        )
        self.plugin_manager = PluginManager(config, cache=cache, api=api, matcher=self)

    # ---- sink ----

    def emit(self, event):
        for sink in list(self.sinks):
            try:
                sink(event)
            except Exception:
                pass

    def _status(self, text, level="info"):
        with self._lock:
            self.emit({
                "type": "status", "text": text, "level": level,
                "players": len(self._players),
                "instance": self.world_name or self.location,
            })

    def _emit_matches(self):
        with self._lock:
            self.emit({
                "type": "matches",
                "matches": [dict(m, rules=list(m["rules"])) for m in self._matches.values()],
                "players": len(self._players),
            })

    # ---- 生命周期 ----

    def _get_scan_pool(self):
        with self._lock:
            if self._scan_pool is None or getattr(self._scan_pool, "_shutdown", False):
                self._scan_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ScanWorker")
            return self._scan_pool

    def start(self):
        self._scan_thread = threading.Thread(target=self._scan_loop, daemon=True, name="Scanner")
        self._scan_thread.start()
        self._avatar_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="Avatar")
        self._scan_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ScanWorker")
        self._status("监控中，等待进入实例…")

    def stop(self):
        self._stop.set()
        self._wakeup.set()
        if hasattr(self, "_avatar_pool") and self._avatar_pool:
            self._avatar_pool.shutdown(wait=False)
        if hasattr(self, "_scan_pool") and self._scan_pool:
            self._scan_pool.shutdown(wait=False)

    def reload_settings(self):
        self.debounce = float(self.config.get("scan", "debounce_seconds", default=3.0) or 3.0)
        self.profile_cache_hours = float(self.config.get("api", "profile_cache_hours", default=4) or 4)
        if hasattr(self, "plugin_manager"):
            self.plugin_manager.reload()

    def set_api(self, api):
        """替换 API 客户端（设置里换了 Cookie 后调用）：好友/身份缓存一并失效。"""
        self.api = api
        self._me_uid = ""
        self._friends = {}
        self._friends_at = 0.0
        if hasattr(self, "plugin_manager"):
            self.plugin_manager.api = api

    # ---- 日志事件入口 ----

    def handle_log_event(self, ev):
        kind = ev.get("kind")
        if kind == "world":
            with self._lock:
                self.location = ev.get("location", "")
                if ev.get("world_id"):
                    self.location = self.location or ev["world_id"]
                self._players.clear()
                self._matches.clear()
                self._profiles.clear()
                self.world_name = ""
            self._status("进入新实例，正在收集玩家…")
            self._emit_matches()
            self._schedule_scan()
        elif kind == "world_name":
            with self._lock:
                self.world_name = ev.get("name", "")
            self._status(None)  # 刷新实例名显示
        elif kind == "join":
            key = ev.get("user_id") or ("name:" + ev.get("name", ""))
            with self._lock:
                self._players[key] = {"name": ev.get("name", ""), "user_id": ev.get("user_id", "")}
            self._schedule_scan()
        elif kind == "leave":
            key = ev.get("user_id") or ("name:" + ev.get("name", ""))
            with self._lock:
                self._players.pop(key, None)
                uid = ev.get("user_id")
                if uid and uid in self._matches:
                    del self._matches[uid]
            self._emit_matches()
            self._status(None)
        elif kind == "local":
            with self._lock:
                if ev.get("user_id"):
                    self._local_uid = ev["user_id"]
                if ev.get("name"):
                    self._local_name = ev["name"]
                # 日志里只有名字没有id时，把自己从匹配里去掉（按名字）
                self._matches.pop(self._local_uid, None)
                self._matches.pop("name:" + self._local_name, None)

    def _schedule_scan(self):
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce, self._wakeup.set)
            self._timer.daemon = True
            self._timer.start()

    def rescan(self, immediate=True):
        """重新对当前实例内的玩家运行匹配流水线（规则修改或配置更新后调用）。"""
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            if self._scan_thread and self._scan_thread.is_alive():
                self._wakeup.set()
            else:
                self.run_scan()

    # ---- 扫描 ----

    def _scan_loop(self):
        while not self._stop.is_set():
            self._wakeup.wait()
            self._wakeup.clear()
            if self._stop.is_set():
                return
            try:
                self.run_scan()
            except Exception as exc:
                self._status("扫描出错: %s" % exc, level="error")

    def run_scan(self):
        with self._lock:
            snapshot = list(self._players.values())
        if not snapshot:
            with self._lock:
                if self._matches:
                    self._matches.clear()
                    self._emit_matches()
            return

        has_api = self.api is not None and self.api.configured
        if not has_api and not self._profiles:
            self._cookie_error = True
            self._status("未配置 VRChat Cookie，无法获取资料（设置里填 auth cookie）", level="warn")
            return

        self._cookie_error = False
        self._status("正在拉取 %d 位玩家资料…" % len(snapshot))
        self._scanning = True
        try:
            self._ensure_friends()
            excluded = self._compute_excluded()
            active_uids = {p.get("user_id") for p in snapshot if p.get("user_id")}
            enabled_rules_map = {r.id: r for r in self._enabled_rules()}
            with self._lock:
                for uid in list(self._matches.keys()):
                    if uid not in active_uids or uid in excluded:
                        del self._matches[uid]
                    else:
                        entry = self._matches[uid]
                        new_rules = []
                        for r in entry.get("rules", []):
                            rid = r.get("id")
                            if r.get("type") == "plugin":
                                new_rules.append(r)
                            elif rid in enabled_rules_map:
                                er = enabled_rules_map[rid]
                                r["name"] = er.name
                                r["color"] = er.color
                                new_rules.append(r)
                        if new_rules:
                            entry["rules"] = new_rules
                        else:
                            del self._matches[uid]
            self._refresh_exclude_names(snapshot)

            # ---- 阶段 1：优先利用已有信息（内存缓存/本地SQLite缓存/已知昵称）立即进行初筛匹配并推送 ----
            mutual_rules = self._enabled_rules(MUTUAL)
            me = self._get_me_uid() if mutual_rules else ""

            for player in snapshot:
                uid = player.get("user_id")
                if not uid or uid in excluded:
                    continue
                # 先看是否已有缓存资料
                cached_profile = None
                with self._lock:
                    if uid in self._profiles:
                        cached_profile = self._profiles[uid]
                if not cached_profile:
                    cached_data, fetched_at = self.cache.get_profile(uid)
                    if cached_data and time.time() - fetched_at < self.profile_cache_hours * 3600:
                        cached_profile = cached_data
                        with self._lock:
                            self._profiles[uid] = cached_profile

                if cached_profile:
                    self._apply_local_rules(uid, cached_profile)
                    self._apply_plugin_rules(uid, cached_profile)
                elif player.get("name"):
                    # 尚未拉取到完整资料时，先用已知的名字与用户基础信息匹配
                    temp_profile = {"displayName": player["name"], "bio": "", "bioLinks": []}
                    self._apply_local_rules(uid, temp_profile)
                    self._apply_plugin_rules(uid, temp_profile)

                if mutual_rules and me:
                    self._apply_mutual_rules_for_player(player, excluded, mutual_rules, me, cached_only=True)

            # 初筛完成后立刻通知悬浮窗刷新（零等待，缓存数据毫秒级展示）
            self._emit_matches()
            self._request_avatars()

            # ---- 阶段 2：并发拉取最新资料并实时匹配更新（受 api._throttle 严格控速，消除单连接网络延迟阻塞） ----
            valid_targets = [p for p in snapshot if p.get("user_id") and p["user_id"] not in excluded]
            skipped = len(snapshot) - len(valid_targets)
            total_targets = len(valid_targets)

            fetched, failed = 0, 0
            with self._lock:
                self._failed_uids = set()

            if valid_targets:
                def _fetch_worker(player):
                    if self._stop.is_set():
                        return None, False
                    uid = player["user_id"]
                    with self._lock:
                        self._fetching_uids.add(uid)
                    success = False
                    try:
                        profile = self._get_profile(uid)
                        if self._stop.is_set():
                            return uid, False

                        if profile is None:
                            with self._lock:
                                self._failed_uids.add(uid)
                        else:
                            with self._lock:
                                self._profiles[uid] = profile
                            self._apply_local_rules(uid, profile)
                            self._apply_plugin_rules(uid, profile)
                            success = True

                        if mutual_rules and me and not self._stop.is_set():
                            self._apply_mutual_rules_for_player(player, excluded, mutual_rules, me, cached_only=False)

                        return uid, success
                    finally:
                        with self._lock:
                            self._fetching_uids.discard(uid)

                pool = self._get_scan_pool()
                futures = [pool.submit(_fetch_worker, p) for p in valid_targets]
                for fut in as_completed(futures):
                    if self._stop.is_set():
                        break
                    try:
                        uid, succ = fut.result()
                        if succ:
                            fetched += 1
                        else:
                            failed += 1
                    except Exception:
                        failed += 1

                    total_done = fetched + failed
                    if total_done % 5 == 0 or total_done == total_targets:
                        self._status("拉取资料中… %d/%d" % (total_done, total_targets))

                    # 关键：每拉取并匹配完一位玩家，立即推送更新到界面，无需等待全部玩家完成
                    self._emit_matches()
                    self._request_avatars()

            # ---- 阶段 3：LLM 大模型规则批量计算 ----
            self._run_llm_rules()
            with self._lock:
                count = len(self._matches)
            level = "warn" if failed else "info"
            excluded_note = "，排除 %d 人" % skipped if skipped else ""
            self._status("扫描完成：实例内 %d 人%s，匹配 %d 人"
                         % (len(snapshot), excluded_note, count), level=level)
            self._emit_matches()
            self._request_avatars()
        finally:
            self._scanning = False
            self._fetching_uid = None
            with self._lock:
                self._fetching_uids.clear()
            self.last_scan_time = time.time()

    def _get_profile(self, uid):
        with self._lock:
            if uid in self._profiles:
                return self._profiles[uid]
        cached, fetched_at = self.cache.get_profile(uid)
        now = time.time()
        if cached and now - fetched_at < self.profile_cache_hours * 3600:
            return cached  # 简介缓存按时间过期（默认 4 小时），期内直接复用
        if not self.api or not self.api.configured:
            return cached
        try:
            full = self.api.get_profile(uid)
        except VRChatAPIError as exc:
            if exc.status == 401:
                self._status("Cookie 无效或已过期，请在设置里重新获取", level="error")
                raise
            self._status("获取 %s 失败: %s" % (uid, exc), level="warn")
            return cached  # API 失败时退回过期缓存也比没有强
        profile = slim_profile(full)
        profile["_fetched_at"] = time.time()
        self.cache.put_profile(uid, profile, max_age_seconds=self.profile_cache_hours * 3600)
        return profile

    def _apply_local_rules(self, uid, profile):
        """跑所有本地规则并更新该用户的匹配结果。"""
        with self._lock:
            entry = self._matches.get(uid) or self._new_entry(uid, profile)
            if profile and (not entry.get("profile", {}).get("avatar") or profile.get("avatar")):
                entry["profile"] = profile
            if profile.get("displayName"):
                entry["name"] = profile["displayName"]
            old_rules = list(entry.get("rules", []))
        hit_rules = []
        for rule in self._enabled_rules(KEYWORD, REGEX):
            hits = rules_mod.match_local(rule, profile)
            if hits:
                hit_rules.append({
                    "id": rule.id, "name": rule.name, "type": rule.type,
                    "reason": "、".join(hits[:5]), "color": rule.color,
                    "action": {"type": "detail"},
                })
        enabled_rules_map = {r.id: r for r in self._enabled_rules()}
        preserved = []
        for r in old_rules:
            if r.get("type") not in (KEYWORD, REGEX):
                rid = r.get("id")
                if r.get("type") == "plugin" or r.get("plugin_id") or rid in enabled_rules_map:
                    if rid in enabled_rules_map:
                        er = enabled_rules_map[rid]
                        r["name"] = er.name
                        r["color"] = er.color
                    preserved.append(r)
        self._update_entry(uid, entry, hit_rules + preserved)

    def _apply_plugin_rules(self, uid, profile):
        """跑用户添加并启用的插件规则并更新该用户的匹配结果。"""
        if not hasattr(self, "plugin_manager"):
            return
        uinfo = PluginUserInfo.from_profile(uid, profile, context=self.plugin_manager)
        plugin_hits = self.plugin_manager.match_external_plugins(uinfo, enabled_rules=self._enabled_rules())
        with self._lock:
            entry = self._matches.get(uid) or self._new_entry(uid, profile)
            if profile and (not entry.get("profile", {}).get("avatar") or profile.get("avatar")):
                entry["profile"] = profile
            if profile.get("displayName"):
                entry["name"] = profile["displayName"]
            other_rules = [r for r in entry.get("rules", []) if r.get("type") in (KEYWORD, REGEX, MUTUAL, LLM, EXCLUDE)]
            self._update_entry(uid, entry, other_rules + plugin_hits)

    def _run_llm_rules(self):
        llm_rules = self._enabled_rules(LLM)
        if not llm_rules:
            return
        with self._lock:
            profiles = dict(self._profiles)
        users = []
        for uid, profile in profiles.items():
            if profile.get("bio") or profile.get("bioLinks"):
                users.append({
                    "user_id": uid,
                    "displayName": profile.get("displayName", ""),
                    "bio": profile.get("bio", ""),
                    "bioLinks": profile.get("bioLinks") or [],
                    "pronouns": profile.get("pronouns", ""),
                    "tags": profile.get("tags") or [],
                })
        if not users:
            return

        for rule in llm_rules:
            self._status("LLM 规则「%s」匹配中…" % rule.name)
            try:
                results = self.llm.match_rule(
                    rule, users,
                    progress=lambda done, total: self._status(
                        "LLM 规则「%s」%d/%d 人" % (rule.name, done, total)),
                )
            except LLMNotConfigured:
                if not self._llm_warned:
                    self._llm_warned = True
                    self._status("LLM 规则已跳过：未配置 API Key（不影响本地规则）", level="warn")
                return
            except Exception as exc:
                self._status("LLM 规则「%s」失败: %s" % (rule.name, exc), level="warn")
                continue

            for uid, res in results.items():
                profile = profiles.get(uid)
                with self._lock:
                    entry = self._matches.get(uid) or self._new_entry(uid, profile or {})
                hit_rules = [r for r in entry["rules"] if r["id"] != rule.id]
                if res.get("match"):
                    hit_rules.append({
                        "id": rule.id, "name": rule.name, "type": LLM,
                        "reason": res.get("reason", ""), "color": rule.color,
                        "action": {"type": "detail"},
                    })
                self._update_entry(uid, entry, hit_rules)
            self._emit_matches()

    # ---- 排除规则 ----

    def _compute_excluded(self):
        """所有启用中的排除规则命中的 uid 集合（自己永远在其中，避免匹配到自己）。"""
        excluded = {self._local_uid} if self._local_uid else set()
        for rule in self._enabled_rules(EXCLUDE):
            if rule.exclude_self and self._local_uid:
                excluded.add(self._local_uid)
            if rule.exclude_friends:
                with self._lock:
                    excluded.update(self._friends.keys())
            for u in rule.users:
                uid = u.get("user_id")
                if uid:
                    excluded.add(uid)
        return excluded

    def _refresh_exclude_names(self, snapshot):
        """排除名单按 uid 匹配；在实例里见到名单成员时同步一次最新名字。"""
        changed = False
        by_uid = {p.get("user_id"): p.get("name", "") for p in snapshot if p.get("user_id")}
        for data in self.config.rules:
            if not isinstance(data, dict) or data.get("type") != EXCLUDE:
                continue
            for u in (data.get("users") or []):
                uid = u.get("user_id")
                new_name = by_uid.get(uid)
                if uid and new_name and u.get("name") != new_name:
                    u["name"] = new_name
                    changed = True
        if changed:
            self.config.schedule_save()

    def _ensure_friends(self):
        """有排除规则需要好友数据时，确保好友列表已加载（过期自动刷新）。"""
        if not any(r.exclude_friends for r in self._enabled_rules(EXCLUDE)):
            return
        if self._friends_loading:
            return
        if self._friends and time.time() - self._friends_at < FRIENDS_TTL:
            return
        self._friends_loading = True
        threading.Thread(target=self._fetch_friends, daemon=True, name="FriendsFetch").start()

    def _fetch_friends(self):
        try:
            friends = self.api.get_friends()
            with self._lock:
                self._friends = {
                    u.get("id", ""): u.get("displayName", "")
                    for u in friends if isinstance(u, dict) and u.get("id")
                }
                self._friends_at = time.time()
            self._status("好友列表已加载：%d 人" % len(self._friends))
            self._schedule_scan()  # 好友数据到位后重扫一次，让排除立即生效
        except VRChatAPIError as exc:
            if exc.status == 401:
                self._status("Cookie 无效或已过期，无法加载好友列表", level="error")
            else:
                self._status("好友列表加载失败: %s" % exc, level="warn")
        finally:
            self._friends_loading = False

    # ---- 共同好友规则 ----

    def _apply_mutual_rules_for_player(self, player, excluded, mutual_rules=None, me=None, cached_only=False):
        """对单个玩家评估共同好友规则。cached_only=True 时仅读缓存不发起网络请求。"""
        if mutual_rules is None:
            mutual_rules = self._enabled_rules(MUTUAL)
        if not mutual_rules:
            return
        if not me:
            me = self._get_me_uid()
        if not me:
            return
        uid = player.get("user_id")
        if not uid or uid in excluded:
            return

        with self._lock:
            profile = self._profiles.get(uid) or {}
            entry = self._matches.get(uid) or self._new_entry(uid, profile)
            if not entry.get("name"):
                entry["name"] = player.get("name", "")
            existing_rules = list(entry.get("rules", []))

        other_rules = [r for r in existing_rules if r.get("type") != MUTUAL]
        existing_mutual_map = {r.get("id"): r for r in existing_rules if r.get("type") == MUTUAL}

        new_mutual_rules = {}
        for rule in mutual_rules:
            limit_users = getattr(rule, "limit_users", False)
            target_uids = {u["user_id"] for u in (rule.users or []) if u.get("user_id")} if limit_users else None
            if limit_users:
                if cached_only:
                    friends, fetched_at = self.cache.get_mutual_friends(me, uid)
                    if friends is None or time.time() - fetched_at >= MUTUAL_TTL:
                        if rule.id in existing_mutual_map:
                            new_mutual_rules[rule.id] = existing_mutual_map[rule.id]
                        continue
                else:
                    friends = self._get_mutual_friends_list(me, uid)
                if friends is None:
                    if rule.id in existing_mutual_map:
                        new_mutual_rules[rule.id] = existing_mutual_map[rule.id]
                    continue
                matched_count = sum(1 for f in friends if (f.get("id") or f.get("userId")) in target_uids)
                hit = compare_count(rule.operator, matched_count, rule.count)
                actual_count = matched_count
                reason = "指定共同好友 %d 人" % matched_count
            else:
                if cached_only:
                    count, fetched_at = self.cache.get_mutual_count(me, uid)
                    if count is None or time.time() - fetched_at >= MUTUAL_TTL:
                        if rule.id in existing_mutual_map:
                            new_mutual_rules[rule.id] = existing_mutual_map[rule.id]
                        continue
                else:
                    count = self._get_mutual_count(me, uid)
                if count is None:
                    if rule.id in existing_mutual_map:
                        new_mutual_rules[rule.id] = existing_mutual_map[rule.id]
                    continue
                hit = compare_count(rule.operator, count, rule.count)
                actual_count = count
                reason = "共同好友 %d 人" % count

            if hit:
                display_name = format_rule_display_name(rule.name, actual_count)
                new_mutual_rules[rule.id] = {
                    "id": rule.id, "name": display_name, "raw_name": rule.name, "type": MUTUAL,
                    "reason": reason, "color": rule.color,
                    "action": {"type": "detail"},
                }

        final_rules = other_rules + list(new_mutual_rules.values())
        self._update_entry(uid, entry, final_rules)

    def _run_mutual_rules(self, snapshot, excluded):
        rules = self._enabled_rules(MUTUAL)
        if not rules:
            return
        me = self._get_me_uid()
        if not me:
            return
        targets = [p for p in snapshot
                   if p.get("user_id") and p["user_id"] not in excluded]
        if not targets:
            return

        for rule in rules:
            self._status("共同好友规则「%s」计算中…" % rule.name)
        for player in targets:
            self._apply_mutual_rules_for_player(player, excluded, rules, me, cached_only=False)
        self._emit_matches()

    def _get_me_uid(self):
        """Cookie 账号的用户 id（共同好友数以它为基准），进程内缓存。"""
        if self._me_uid:
            return self._me_uid
        if not self.api or not self.api.configured:
            return ""
        try:
            me = self.api.get_me()
        except VRChatAPIError as exc:
            if exc.status == 401:
                self._status("Cookie 无效或已过期，请在设置里重新获取", level="error")
                raise
            self._status("获取账号信息失败: %s" % exc, level="warn")
            return ""
        self._me_uid = me.get("id", "")
        return self._me_uid

    def _get_mutual_count(self, me, uid):
        """共同好友数：先查缓存（24h 过期），未命中调 API。失败时退回旧缓存。"""
        cached, fetched_at = self.cache.get_mutual_count(me, uid)
        if cached is not None and time.time() - fetched_at < MUTUAL_TTL:
            return cached
        try:
            data = self.api.get_mutual_counts(uid)
        except VRChatAPIError as exc:
            if exc.status == 401:
                raise
            self._status("获取共同好友数失败: %s" % exc, level="warn")
            return cached
        count = parse_mutual_count(data)
        if count is None:
            return cached
        self.cache.put_mutual_count(me, uid, count)
        return count

    def _get_mutual_friends_list(self, me, uid):
        """获取公开可见共同好友列表：先查缓存（24h 过期），未命中调 API。失败时退回旧缓存。"""
        cached, fetched_at = self.cache.get_mutual_friends(me, uid)
        if cached is not None and time.time() - fetched_at < MUTUAL_TTL:
            return cached
        try:
            data = self.api.get_mutual_friends(uid)
        except VRChatAPIError as exc:
            if exc.status == 401:
                raise
            self._status("获取共同好友列表失败: %s" % exc, level="warn")
            return cached
        if not isinstance(data, list):
            return cached
        self.cache.put_mutual_friends(me, uid, data)
        return data

    # ---- 状态表（主界面"状态"页用） ----

    def get_roster(self):
        """当前实例内所有玩家的状态：名字、检测状态、命中的规则（含零命中的）。"""
        with self._lock:
            roster = []
            for p in self._players.values():
                uid = p.get("user_id", "")
                entry = {
                    "user_id": uid,
                    "name": p.get("name", ""),
                    "rules": [],
                    "status": "等待检测",
                    "bio": "",
                }
                if uid and uid == self._local_uid:
                    entry["status"] = "本机"
                elif uid and uid in self._compute_excluded():
                    entry["status"] = "已排除"
                elif not uid:
                    entry["status"] = "无法查询"
                elif uid in self._failed_uids:
                    entry["status"] = "获取失败"
                elif uid in self._profiles:
                    entry["status"] = "已检测"
                elif self._scanning and (uid in self._fetching_uids or self._fetching_uid == uid):
                    entry["status"] = "检测中"
                profile = self._profiles.get(uid) or {}
                entry["bio"] = re.sub(r"\s+", " ", profile.get("bio") or "").strip()
                match = self._matches.get(uid)
                if match:
                    entry["rules"] = [
                        {"name": r.get("name", ""), "reason": r.get("reason", "")}
                        for r in match.get("rules", [])
                    ]
                roster.append(entry)
            return roster

    def get_summary(self):
        """状态页顶栏信息：实例名、人数、匹配数、上次扫描时间。"""
        with self._lock:
            return {
                "instance": self.world_name or self.location or "",
                "players": len(self._players),
                "matched": len(self._matches),
                "last_scan": self.last_scan_time,
                "scanning": self._scanning,
            }

    # ---- 匹配表维护 ----

    def _enabled_rules(self, *types):
        out = []
        for data in self.config.rules:
            if not isinstance(data, dict):
                continue
            rule = Rule.from_dict(data)
            if rule.enabled and (not types or rule.type in types):
                out.append(rule)
        return out

    def _new_entry(self, uid, profile):
        return {
            "user_id": uid,
            "name": profile.get("displayName", ""),
            "rules": [],
            "profile": profile,
        }

    def _update_entry(self, uid, entry, hit_rules):
        with self._lock:
            if hit_rules:
                entry["rules"] = hit_rules
                self._matches[uid] = entry
            else:
                self._matches.pop(uid, None)

    # ---- 头像 ----

    def _request_avatars(self):
        with self._lock:
            items = []
            for uid, m in self._matches.items():
                p = self._profiles.get(uid) or m.get("profile") or {}
                url = p.get("avatar", "")
                if url:
                    items.append((uid, url))
        for uid, url in items:
            self._avatar_pool.submit(self._fetch_avatar, uid, url)

    def _fetch_avatar(self, uid, url):
        os.makedirs(self.avatar_dir, exist_ok=True)
        path = os.path.join(self.avatar_dir, hashlib.md5(uid.encode()).hexdigest() + ".png")
        data = None
        if os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                data = None
        if not data:
            headers = {}
            if self.api and hasattr(self.api, "_headers"):
                headers["User-Agent"] = self.api._headers.get("User-Agent") or "profile-check/1.0"
            else:
                headers["User-Agent"] = "profile-check/1.0 (personal VRChat profile matcher; contact: local-user)"
            cookies = {}
            if self.api and getattr(self.api, "auth_cookie", None):
                cookies["auth"] = self.api.auth_cookie
                if getattr(self.api, "two_factor_cookie", None):
                    cookies["twoFactorAuth"] = self.api.two_factor_cookie
            try:
                resp = requests.get(url, headers=headers, cookies=cookies, timeout=15)
                if resp.status_code == 200 and resp.content:
                    data = resp.content
                    with open(path, "wb") as f:
                        f.write(data)
            except Exception:
                data = None
        if data:
            self.emit({"type": "avatar", "user_id": uid, "data": data})
