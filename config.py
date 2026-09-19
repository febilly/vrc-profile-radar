"""配置读写。首次运行时在程序目录生成 config.json（含示例规则）。

自带防丢保护：每次保存都会在 backups/ 里镜像一份最新配置（config-latest.json）
和每日快照（保留 7 天）；config.json 丢失或损坏时，启动会自动从最近的备份恢复，
规则不会因为文件被误删而丢失。
"""
import json
import os
import shutil
import threading
import time
import uuid

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
CACHE_PATH = os.path.join(APP_DIR, "cache.db")
AVATAR_DIR = os.path.join(APP_DIR, "cache", "avatars")

DEFAULT_LOG_DIR = os.path.join(
    os.environ.get("USERPROFILE", ""), "AppData", "LocalLow", "VRChat", "VRChat", "Output Log"
)

EXAMPLE_RULES = [
    {
        "id": "example-keyword",
        "name": "日语学习者",
        "type": "keyword",
        "keywords": ["日语", "日本語", "日本語勉強中", "japanese", "日本語話せる"],
        "mode": "any",
        "fields": ["bio"],
        "enabled": True,
    },
    {
        "id": "example-regex",
        "name": "有X(推特)链接",
        "type": "regex",
        "pattern": "(?:twitter|x)\\.com/[A-Za-z0-9_]{1,20}",
        "fields": ["bio", "bioLinks"],
        "enabled": True,
    },
    {
        "id": "example-exclude",
        "name": "排除",
        "type": "exclude",
        "exclude_self": True,
        "exclude_friends": False,
        "users": [],
        "enabled": True,
    },
    {
        "id": "example-llm",
        "name": "VRChat创作者(AI判断)",
        "type": "llm",
        "prompt": "判断该用户是否是 VRChat 相关的内容创作者（例如：模型/地图制作者、主播、视频作者、摄影师、程序员）。只要资料中有明显迹象即算符合。",
        "enabled": True,
    },
]

DEFAULT_CONFIG = {
    "cookies": {
        "auth": "",
        "twoFactorAuth": "",
    },
    "api": {
        "requests_per_second": 2.0,
        "profile_cache_hours": 4,
    },
    "llm": {
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
        "batch_size": 10,
        "concurrency": 2,
        "timeout_seconds": 90,
        "result_cache_max": 10000,
    },
    "scan": {
        "debounce_seconds": 3.0,
    },
    "rules": EXAMPLE_RULES,
    "ui": {
        "always_on_top": True,
        "alpha": 0.9,
        "show_avatars": True,
        "overlay_x": -1,
        "overlay_y": -1,
        "main_w": 760,
        "main_h": 600,
    },
    "log_dir": "",
}


class Config:
    def __init__(self, path=None):
        self.path = path or CONFIG_PATH
        self.backup_dir = os.path.join(os.path.dirname(os.path.abspath(self.path)), "backups")
        self._lock = threading.RLock()
        self._save_timer = None
        self.data = {}
        self.load()

    def load(self):
        with self._lock:
            loaded = False
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        self.data = json.load(f)
                    loaded = True
                except (OSError, ValueError):
                    # 文件损坏：留档一份供检查，按丢失处理
                    try:
                        os.replace(self.path, "%s.corrupt-%d" % (self.path, int(time.time())))
                    except OSError:
                        pass
            if not loaded and self._restore_from_backup():
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        self.data = json.load(f)
                    loaded = True
                except (OSError, ValueError):
                    self.data = {}
            if not loaded:
                self.data = {}
            if not isinstance(self.data, dict):
                self.data = {}
            self._fill_defaults(self.data, DEFAULT_CONFIG)
            if not os.path.exists(self.path):
                self.save()

    def _restore_from_backup(self):
        """config.json 丢失或损坏时，用最新的备份（backups/config-latest.json 优先）恢复。"""
        candidates = []
        latest = os.path.join(self.backup_dir, "config-latest.json")
        if os.path.exists(latest):
            candidates.append(latest)
        if os.path.isdir(self.backup_dir):
            for name in os.listdir(self.backup_dir):
                if name.startswith("config-2") and name.endswith(".json"):
                    candidates.append(os.path.join(self.backup_dir, name))
        if not candidates:
            return False
        newest = max(candidates, key=os.path.getmtime)
        try:
            shutil.copyfile(newest, self.path)
            return True
        except OSError:
            return False

    def _fill_defaults(self, target, defaults):
        for key, value in defaults.items():
            if key not in target:
                target[key] = json.loads(json.dumps(value))
            elif isinstance(value, dict) and isinstance(target[key], dict):
                self._fill_defaults(target[key], value)

    def save(self):
        with self._lock:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
            finally:
                self._mirror_backup()

    def _mirror_backup(self):
        """保存后镜像一份到 backups/：config-latest.json 永远是最后一次保存的状态，
        另外每天留一份快照（保留 7 天），供 config.json 被误删/损坏时恢复。"""
        try:
            os.makedirs(self.backup_dir, exist_ok=True)
            shutil.copyfile(self.path, os.path.join(self.backup_dir, "config-latest.json"))
            daily = os.path.join(self.backup_dir, "config-%s.json" % time.strftime("%Y-%m-%d"))
            if not os.path.exists(daily):
                shutil.copyfile(self.path, daily)
            snaps = sorted(
                (n for n in os.listdir(self.backup_dir)
                 if n.startswith("config-2") and n.endswith(".json"))
            )
            for name in snaps[:-7]:
                os.remove(os.path.join(self.backup_dir, name))
        except OSError:
            pass

    def get(self, *keys, default=None):
        with self._lock:
            node = self.data
            for key in keys:
                if not isinstance(node, dict) or key not in node:
                    return default
                node = node[key]
            return node

    def set(self, value, *keys, defer=False):
        """写入配置；defer=True 时内存立即生效、写盘防抖合并（用于输入框逐键回调）。"""
        with self._lock:
            node = self.data
            for key in keys[:-1]:
                node = node.setdefault(key, {})
            node[keys[-1]] = value
            if defer:
                self.schedule_save()
            else:
                self.save()

    def schedule_save(self, delay=0.4):
        """合并短时间内的多次更改，延迟统一写盘；内存中的 data 始终是最新值。"""
        with self._lock:
            if self._save_timer:
                self._save_timer.cancel()
        timer = threading.Timer(delay, self.save)
        timer.daemon = True
        with self._lock:
            self._save_timer = timer
        timer.start()

    @property
    def rules(self):
        return self.get("rules", default=[])

    @property
    def auth_cookie(self):
        return (self.get("cookies", "auth", default="") or "").strip()

    @property
    def two_factor_cookie(self):
        return (self.get("cookies", "twoFactorAuth", default="") or "").strip()

    def new_rule_id(self):
        return "rule-" + uuid.uuid4().hex[:8]
