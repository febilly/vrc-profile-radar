"""VRChat 日志监控。

日志格式与 VRCX (github.com/vrcx-team/VRCX, Dotnet/LogWatcher.cs) 一致：
    2024.01.01 12:34:56 Log        -  [Behaviour] OnPlayerJoined 名字 (usr_xxxx)
    2024.01.01 12:34:56 Log        -  [Behaviour] OnPlayerLeft 名字 (usr_xxxx)
    2024.01.01 12:34:56 Log        -  [Behaviour] Joining wrld_xxxx:12345~region(jp)
    2024.01.01 12:34:56 Log        -  [Behaviour] Entering Room: 世界名
    2024.01.01 12:34:56 Log        -  [Player] Initialized PlayerAPI "名字" is local
    2024.01.01 12:34:56 Log        -  [API] User Authenticated: 名字 (usr_xxxx)

默认日志位置（自动兼容新旧两版）:
    新版: %USERPROFILE%\\AppData\\LocalLow\\VRChat\\VRChat\\output_log_*.txt
    旧版: %USERPROFILE%\\AppData\\LocalLow\\VRChat\\VRChat\\Output Log\\output_log_*.txt
"""
import os
import re
import threading
import time

LINE_RE = re.compile(r"^(\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2})\s+\S+\s+-\s+(.*)$")
CLEAN_ID_RE = re.compile(r"[^a-zA-Z0-9_\-~:()]")
WORLD_RE = re.compile(r"wrld_[0-9a-fA-F-]{8,}")

JOIN_TAG = "] OnPlayerJoined"
LEFT_TAG = "] OnPlayerLeft"
JOINING_TAG = "] Joining "
ENTERING_TAG = "] Entering Room: "


def default_log_dir():
    """新版 VRChat（ECP 更新后）的日志目录（官方文档 docs.vrchat.com "Local VRChat Storage"）：
    %USERPROFILE%\\AppData\\LocalLow\\VRChat\\VRChat\\，日志文件直接放在该目录下；
    旧版 VRChat 放在其子目录 Output Log\\ 里。latest_log_file 会同时搜索两处。"""
    return os.path.join(
        os.environ.get("USERPROFILE", ""), "AppData", "LocalLow", "VRChat", "VRChat"
    )


def _log_search_dirs(log_dir=None):
    """返回要搜索日志的目录列表：给定目录本身 + 其 Output Log 子目录（旧版位置）。
    log_dir 为空时用新版 VRChat 根目录，新旧两个位置都会被搜索。"""
    base = log_dir or default_log_dir()
    return [base, os.path.join(base, "Output Log")]


def _log_file_sort_key(path):
    """排序键：优先用文件名内嵌的时间戳（output_log_YYYY-MM-DD_HH-MM-SS.txt），
    比文件创建时间可靠；文件名里没有时间戳时退回创建时间。"""
    m = re.search(
        r"output_log_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})", os.path.basename(path)
    )
    if m:
        return (m.group(1), m.group(2), m.group(3), m.group(4))
    try:
        return ("", "", "", time.strftime(
            "%Y-%m-%d %H-%M-%S", time.localtime(os.path.getctime(path))))
    except OSError:
        return ("", "", "", "")


def latest_log_file(log_dir=None):
    """跨所有候选目录找最新的 output_log_*.txt；没有则返回 None。"""
    newest = None
    newest_key = None
    for d in _log_search_dirs(log_dir):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if not (name.startswith("output_log_") and name.endswith(".txt")):
                continue
            path = os.path.join(d, name)
            key = _log_file_sort_key(path)
            if newest_key is None or key > newest_key:
                newest, newest_key = path, key
    return newest


def parse_user_info(text):
    """'名字 (usr_xxx)' -> (名字, usr_xxx)；无 id 时 -> (原文, '')。与 VRCX ParseUserInfo 一致。"""
    text = text.strip()
    pos = text.rfind(" (")
    if pos >= 0:
        name = text[:pos].strip()
        end = text.rfind(")")
        uid = CLEAN_ID_RE.sub("", text[pos + 2 : end if end > pos else len(text)])
        return name, uid
    return text, ""


def parse_line(line):
    """解析一行日志，返回事件 dict 或 None。

    事件: join{name,user_id} / leave{name,user_id} / world{location,world_id} /
          world_name{name} / local{name,user_id}
    """
    m = LINE_RE.match(line)
    if not m:
        return None
    ts, rest = m.group(1), m.group(2)

    if JOIN_TAG in rest and "] OnPlayerJoined:" not in rest:
        idx = rest.rfind(JOIN_TAG)
        name, uid = parse_user_info(rest[idx + len(JOIN_TAG):])
        if not name and not uid:
            return None
        if not uid and name.lower() in ("room", ""):
            return None
        return {"kind": "join", "name": name, "user_id": uid, "time": ts}

    if LEFT_TAG in rest and "] OnPlayerLeftRoom" not in rest and "] OnPlayerLeft:" not in rest:
        idx = rest.rfind(LEFT_TAG)
        name, uid = parse_user_info(rest[idx + len(LEFT_TAG):])
        if not name and not uid:
            return None
        return {"kind": "leave", "name": name, "user_id": uid, "time": ts}

    if JOINING_TAG in rest and "] Joining or Creating Room: " not in rest and "] Joining friend: " not in rest:
        idx = rest.rfind(JOINING_TAG)
        location = rest[idx + len(JOINING_TAG):].strip()
        world_id_m = WORLD_RE.search(location)
        if not world_id_m:
            return None
        return {
            "kind": "world",
            "location": location.rstrip("/"),
            "world_id": world_id_m.group(0),
            "time": ts,
        }

    if ENTERING_TAG in rest:
        idx = rest.rfind(ENTERING_TAG)
        name = rest[idx + len(ENTERING_TAG):].strip()
        if name:
            return {"kind": "world_name", "name": name, "time": ts}

    lm = re.search(r'Initialized PlayerAPI "(.+)" is local', rest)
    if lm:
        return {"kind": "local", "name": lm.group(1), "user_id": "", "time": ts}

    if "] User Authenticated: " in rest:
        idx = rest.rfind("] User Authenticated: ")
        name, uid = parse_user_info(rest[idx + len("] User Authenticated: "):])
        if name or uid:
            return {"kind": "local", "name": name, "user_id": uid, "time": ts}

    return None


def latest_log_file(log_dir=None):
    """跨所有候选目录找最新的 output_log_*.txt；没有则返回 None。"""
    newest = None
    newest_key = None
    for d in _log_search_dirs(log_dir):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if not (name.startswith("output_log_") and name.endswith(".txt")):
                continue
            path = os.path.join(d, name)
            key = _log_file_sort_key(path)
            if newest_key is None or key > newest_key:
                newest, newest_key = path, key
    return newest


class LogTailer(threading.Thread):
    """跟踪最新日志文件，解析出新事件回调 on_event(event dict)。

    启动时会从当前最新日志的**开头**读一遍，恢复出当前实例里已有的人；
    之后只增量读取。出现更新的日志文件（VRChat 重启）时自动切换并重置状态。
    """

    def __init__(self, on_event, log_dir=None, poll_interval=0.5, start_from_beginning=True):
        super().__init__(daemon=True, name="LogTailer")
        self.on_event = on_event
        # log_dir=None 表示自动搜索：新版根目录 + 旧版 Output Log 子目录都查
        self.log_dir = log_dir or None
        self.poll_interval = poll_interval
        self.start_from_beginning = start_from_beginning
        self._stop = threading.Event()
        self._current_path = None
        self._pos = 0
        self._buf = ""

    def stop(self):
        self._stop.set()

    def _read_new(self, fh):
        """增量读取文件尾部，输出完整行。"""
        fh.seek(self._pos)
        data = fh.read()
        self._pos = fh.tell()
        if not data:
            return []
        self._buf += data
        lines = self._buf.split("\n")
        self._buf = lines.pop()
        return [l.rstrip("\r") for l in lines]

    def _process(self, line):
        event = parse_line(line)
        if event:
            try:
                self.on_event(event)
            except Exception:
                pass

    def run(self):
        while not self._stop.is_set():
            try:
                path = latest_log_file(self.log_dir)
                if path is None:
                    self._stop.wait(self.poll_interval)
                    continue

                if path != self._current_path:
                    self._current_path = path
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        if self.start_from_beginning:
                            self._buf = ""
                            for line in fh:
                                self._process(line.rstrip("\r"))
                            self._pos = fh.tell()
                        else:
                            self._pos = os.path.getsize(path)
                            self._buf = ""
                    continue

                size = os.path.getsize(path)
                if size < self._pos:
                    self._pos = 0
                    self._buf = ""
                    continue

                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    for line in self._read_new(fh):
                        self._process(line)
            except OSError:
                time.sleep(self.poll_interval)
                continue
            self._stop.wait(self.poll_interval)
