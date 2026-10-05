"""SQLite 本地缓存，三套互相独立：

1. 玩家简介缓存（profiles）：按"获取时间"绝对过期（默认 4 小时，可在设置里调），
   过期后重新调用 VRChat API 拉取资料。
2. LLM 结果缓存（llm_results）：把"某段简介 + 某条规则"交给大模型得到的结果存下来，
   只要简介和规则没变就一直复用（不重复花钱）；按数量上限（默认 10000 条）淘汰最久未用的。
3. 共同好友数缓存（mutual_counts）：按 (Cookie 账号, 目标玩家) 存共同好友数量，绝对过期。
"""
import json
import sqlite3
import threading
import time


class CacheStore:
    def __init__(self, path):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS profiles ("
            " user_id TEXT PRIMARY KEY, data TEXT NOT NULL, fetched_at REAL NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS llm_results ("
            " cache_key TEXT PRIMARY KEY, result TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS mutual_counts ("
            " me TEXT NOT NULL, user_id TEXT NOT NULL, count INTEGER NOT NULL,"
            " fetched_at REAL NOT NULL, PRIMARY KEY (me, user_id))"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS mutual_friends ("
            " me TEXT NOT NULL, user_id TEXT NOT NULL, friends TEXT NOT NULL,"
            " fetched_at REAL NOT NULL, PRIMARY KEY (me, user_id))"
        )
        self._conn.commit()

    # ---- 玩家简介缓存（按时间过期，过期判断由调用方按 fetched_at 做） ----

    def get_profile(self, user_id):
        """返回 (资料, 资料的获取时间)。读取不刷新时间：简介按获取时间绝对过期。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT data, fetched_at FROM profiles WHERE user_id = ?", (user_id,)
            ).fetchone()
        if not row:
            return None, 0.0
        try:
            return json.loads(row[0]), row[1]
        except ValueError:
            return None, 0.0

    def put_profile(self, user_id, profile, max_age_seconds=None):
        """写入资料；给 max_age_seconds 时顺手清掉已超过该时长的旧资料。"""
        data = json.dumps(profile, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT INTO profiles (user_id, data, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET data = excluded.data, fetched_at = excluded.fetched_at",
                (user_id, data, time.time()),
            )
            if max_age_seconds is not None and max_age_seconds > 0:
                self._conn.execute(
                    "DELETE FROM profiles WHERE fetched_at < ?",
                    (time.time() - max_age_seconds,),
                )
            self._conn.commit()

    # ---- LLM 结果缓存（按数量上限 + LRU 淘汰） ----

    def get_llm(self, cache_key):
        """命中时刷新使用时间（作为 LRU 淘汰的依据）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT result FROM llm_results WHERE cache_key = ?", (cache_key,)
            ).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE llm_results SET created_at = ? WHERE cache_key = ?",
                    (time.time(), cache_key),
                )
                self._conn.commit()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            return None

    def put_llm(self, cache_key, result, max_count=None):
        """写入 LLM 结果；给 max_count 时淘汰最久未用的条目。"""
        data = json.dumps(result, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT INTO llm_results (cache_key, result, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET result = excluded.result, created_at = excluded.created_at",
                (cache_key, data, time.time()),
            )
            if max_count is not None and max_count > 0:
                (count,) = self._conn.execute("SELECT COUNT(*) FROM llm_results").fetchone()
                extra = count - max_count
                if extra > 0:
                    self._conn.execute(
                        "DELETE FROM llm_results WHERE cache_key IN "
                        "(SELECT cache_key FROM llm_results ORDER BY created_at ASC LIMIT ?)",
                        (extra,),
                    )
            self._conn.commit()

    def clear_llm(self):
        with self._lock:
            self._conn.execute("DELETE FROM llm_results")
            self._conn.commit()

    # ---- 共同好友数缓存（按获取时间绝对过期，过期判断由调用方做） ----

    def get_mutual_count(self, me, user_id):
        """返回 (共同好友数, 获取时间)；无记录时 (None, 0.0)。读取不刷新时间。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT count, fetched_at FROM mutual_counts WHERE me = ? AND user_id = ?",
                (me, user_id),
            ).fetchone()
        if not row:
            return None, 0.0
        return int(row[0]), row[1]

    def put_mutual_count(self, me, user_id, count):
        with self._lock:
            self._conn.execute(
                "INSERT INTO mutual_counts (me, user_id, count, fetched_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(me, user_id) DO UPDATE SET"
                " count = excluded.count, fetched_at = excluded.fetched_at",
                (me, user_id, int(count), time.time()),
            )
            self._conn.commit()

    def get_mutual_friends(self, me, user_id):
        """返回 (共同好友列表, 获取时间)；无记录时 (None, 0.0)。读取不刷新时间。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT friends, fetched_at FROM mutual_friends WHERE me = ? AND user_id = ?",
                (me, user_id),
            ).fetchone()
        if not row:
            return None, 0.0
        try:
            return json.loads(row[0]), row[1]
        except (ValueError, TypeError):
            return None, 0.0

    def put_mutual_friends(self, me, user_id, friends):
        with self._lock:
            self._conn.execute(
                "INSERT INTO mutual_friends (me, user_id, friends, fetched_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(me, user_id) DO UPDATE SET"
                " friends = excluded.friends, fetched_at = excluded.fetched_at",
                (me, user_id, json.dumps(friends, ensure_ascii=False), time.time()),
            )
            self._conn.commit()

    def clear_profiles(self):
        with self._lock:
            self._conn.execute("DELETE FROM profiles")
            self._conn.execute("DELETE FROM mutual_counts")
            self._conn.execute("DELETE FROM mutual_friends")
            self._conn.commit()
