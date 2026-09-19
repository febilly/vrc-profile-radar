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
from concurrent.futures import ThreadPoolExecutor

import requests

import rules as rules_mod
from rules import Rule, KEYWORD, REGEX, LLM, EXCLUDE, MUTUAL, compare_count
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
        self.avatar_dir = os.path.join(os.path.dirname(os.path.abspath(config.path)), "cache", "avatars")

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

    def start(self):
        self._scan_thread = threading.Thread(target=self._scan_loop, daemon=True, name="Scanner")
        self._scan_thread.start()
        self._avatar_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="Avatar")
        self._status("监控中，等待进入实例…")

    def stop(self):
        self._stop.set()
        self._wakeup.set()

    def reload_settings(self):
        self.debounce = float(self.config.get("scan", "debounce_seconds", default=3.0) or 3.0)
        self.profile_cache_hours = float(self.config.get("api", "profile_cache_hours", default=4) or 4)

    def set_api(self, api):
        """替换 API 客户端（设置里换了 Cookie 后调用）：好友/身份缓存一并失效。"""
        self.api = api
        self._me_uid = ""
        self._friends = {}
        self._friends_at = 0.0

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
            return

        if self.api is None or not self.api.configured:
            self._cookie_error = True
            self._status("未配置 VRChat Cookie，无法获取资料（设置里填 auth cookie）", level="warn")
            return

        self._cookie_error = False
        self._status("正在拉取 %d 位玩家资料…" % len(snapshot))
        self._scanning = True
        try:
            self._ensure_friends()
            excluded = self._compute_excluded()
            with self._lock:
                for uid in excluded:
                    self._matches.pop(uid, None)
                    self._profiles.pop(uid, None)
            self._refresh_exclude_names(snapshot)
            fetched, failed, skipped = 0, 0, 0
            with self._lock:
                self._failed_uids = set()
            for i, player in enumerate(snapshot):
                if self._stop.is_set():
                    return
                uid = player["user_id"]
                if not uid:
                    continue  # 旧版日志无 usr id，无法查询
                if uid in excluded:
                    skipped += 1
                    continue
                self._fetching_uid = uid
                profile = self._get_profile(uid)
                self._fetching_uid = None
                if profile is None:
                    failed += 1
                    with self._lock:
                        self._failed_uids.add(uid)
                else:
                    with self._lock:
                        self._profiles[uid] = profile
                    self._apply_local_rules(uid, profile)
                    self._emit_matches()
                fetched += 1
                if fetched % 5 == 0 or i == len(snapshot) - 1:
                    self._status("拉取资料中… %d/%d" % (i + 1, len(snapshot)))

            self._run_llm_rules()
            self._run_mutual_rules(snapshot, excluded)
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
            self.last_scan_time = time.time()

    def _get_profile(self, uid):
        cached, fetched_at = self.cache.get_profile(uid)
        now = time.time()
        if cached and now - fetched_at < self.profile_cache_hours * 3600:
            return cached  # 简介缓存按时间过期（默认 4 小时），期内直接复用
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
        hit_rules = []
        for rule in self._enabled_rules(KEYWORD, REGEX):
            hits = rules_mod.match_local(rule, profile)
            if hits:
                hit_rules.append({
                    "id": rule.id, "name": rule.name, "type": rule.type,
                    "reason": "、".join(hits[:5]), "color": rule.color,
                })
        self._update_entry(uid, entry, hit_rules)

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
                uid = player["user_id"]
                count = self._get_mutual_count(me, uid)
                if count is None:
                    continue  # 获取失败且无缓存：跳过，不动已有匹配
                hit = compare_count(rule.operator, count, rule.count)
                reason = "共同好友 %d 人" % count
                with self._lock:
                    profile = self._profiles.get(uid) or {}
                    entry = self._matches.get(uid) or self._new_entry(uid, profile)
                    if not entry.get("name"):
                        entry["name"] = player.get("name", "")
                hit_rules = [r for r in entry["rules"] if r["id"] != rule.id]
                if hit:
                    hit_rules.append({
                        "id": rule.id, "name": rule.name, "type": MUTUAL,
                        "reason": reason, "color": rule.color,
                    })
                self._update_entry(uid, entry, hit_rules)
            self._emit_matches()

    def _get_me_uid(self):
        """Cookie 账号的用户 id（共同好友数以它为基准），进程内缓存。"""
        if self._me_uid:
            return self._me_uid
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
                elif self._scanning and self._fetching_uid == uid:
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
            items = [(m["user_id"], (m.get("profile") or {}).get("avatar", ""))
                     for m in self._matches.values()]
        for uid, url in items:
            if not url:
                continue
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
            try:
                resp = requests.get(url, timeout=15)
                if resp.status_code == 200:
                    data = resp.content
                    with open(path, "wb") as f:
                        f.write(data)
            except Exception:
                data = None
        if data:
            self.emit({"type": "avatar", "user_id": uid, "data": data})
