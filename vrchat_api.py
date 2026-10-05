"""VRChat API 客户端：用 auth / twoFactorAuth cookie 直接调用，带限流与重试。

接口基址 https://api.vrchat.cloud/api/1
  GET /auth/user            验证 cookie，返回自己的用户对象
  GET /profile/{userId}     返回完整资料（bio、bioLinks、头像缩略图等；/users/{id} 已只给精简对象）
  GET /users?search=        按名字搜索玩家（返回精简对象：id、名字、tags、头像等）
  GET /auth/user/friends    好友列表（offset 分页）
  GET /users/{id}/mutuals   与某玩家的共同好友/群组计数（隐藏的共同好友也计入 friendCount）
VRChat 要求自定义 User-Agent（带联系方式），并明确要求控制请求频率，这里默认 2 req/s，
收到 429 时按 Retry-After 退避并自动降低速率。
"""
import os
import sys
import threading
import time
import urllib.parse
from datetime import datetime

import requests

BASE_URL = "https://api.vrchat.cloud/api/1"
DEFAULT_UA = "profile-check/1.0 (personal VRChat profile matcher; contact: local-user)"


def _init_vt_mode():
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            try:
                os.system("")
            except Exception:
                pass


_init_vt_mode()


def _log_api(method, path, status_code=None, status_text=None, duration_ms=None,
             attempt=0, detail=None):
    """在控制台输出带时间戳和 ANSI 彩色的 VRChat API 请求日志。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    time_str = f"\033[90m[{ts}]\033[0m"
    tag_str = "\033[1;96m[VRChat API]\033[0m"
    attempt_str = f" \033[33m(重试 {attempt}/2)\033[0m" if attempt > 0 else ""
    method_str = f"\033[1;37m{method}\033[0m"
    path_str = f"\033[97m{path}\033[0m"

    if status_code == 200:
        status_fmt = f"\033[1;92m{status_text or '200 OK'}\033[0m"
    elif status_code == 429:
        status_fmt = f"\033[1;93m{status_text or '429 Too Many Requests'}\033[0m"
    elif status_code == 401:
        status_fmt = f"\033[1;91m{status_text or '401 Unauthorized'}\033[0m"
    elif status_code == 404:
        status_fmt = f"\033[33m{status_text or '404 Not Found'}\033[0m"
    elif status_code and status_code >= 500:
        status_fmt = f"\033[1;91m{status_text or f'HTTP {status_code}'}\033[0m"
    elif status_code:
        status_fmt = f"\033[1;93m{status_text or f'HTTP {status_code}'}\033[0m"
    elif status_text:
        status_fmt = f"\033[1;91m{status_text}\033[0m"
    else:
        status_fmt = "\033[90m---\033[0m"

    dur_str = ""
    if duration_ms is not None:
        if duration_ms < 500:
            dur_str = f" \033[90m({duration_ms:.0f}ms)\033[0m"
        elif duration_ms < 2000:
            dur_str = f" \033[33m({duration_ms:.0f}ms)\033[0m"
        else:
            dur_str = f" \033[91m({duration_ms:.0f}ms)\033[0m"

    detail_str = ""
    if detail:
        if status_code == 429 or "429" in (status_text or ""):
            detail_str = f" \033[33m[{detail}]\033[0m"
        elif status_code == 401 or "401" in (status_text or "") or "错误" in (status_text or ""):
            detail_str = f" \033[91m[{detail}]\033[0m"
        else:
            detail_str = f" \033[90m[{detail}]\033[0m"

    print(f"{time_str} {tag_str}{attempt_str} {method_str} {path_str} \033[90m->\033[0m {status_fmt}{dur_str}{detail_str}", flush=True)


class VRChatAPIError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class VRChatAPI:
    def __init__(self, auth_cookie="", two_factor_cookie="", requests_per_second=2.0,
                 user_agent=None, timeout=20, burst=2.0):
        self.auth_cookie = (auth_cookie or "").strip()
        self.two_factor_cookie = (two_factor_cookie or "").strip()
        self.base_requests_per_second = max(0.2, float(requests_per_second))
        self.requests_per_second = self.base_requests_per_second
        self.burst = max(1.0, float(burst))
        self.timeout = timeout
        self._headers = {
            "User-Agent": user_agent or DEFAULT_UA,
            "Accept": "application/json",
        }
        self._lock = threading.Lock()

        # 令牌桶（Token Bucket）限流状态
        self._tokens = self.burst
        self._last_token_time = time.monotonic()
        self._blocked_until = 0.0
        self._consecutive_successes = 0

    def set_cookies(self, auth_cookie, two_factor_cookie=""):
        self.auth_cookie = (auth_cookie or "").strip()
        self.two_factor_cookie = (two_factor_cookie or "").strip()

    @property
    def configured(self):
        return bool(self.auth_cookie)

    def _cookie_header(self):
        pairs = ["auth=" + self.auth_cookie]
        if self.two_factor_cookie:
            pairs.append("twoFactorAuth=" + self.two_factor_cookie)
        return "; ".join(pairs)

    def _throttle(self):
        """标准令牌桶限流算法：
        1. 允许适度突发请求（默认 burst=2.0），空闲后查完资料紧接着查共同好友时可零等待立即发送。
        2. 突发耗尽后平滑受控于 requests_per_second（默认 2 req/s）。
        3. 若触发 429，设置全局 _blocked_until 冷却锁定，避免多线程在此期间再次向 API 施压。
        """
        while True:
            with self._lock:
                now = time.monotonic()

                # 429 全局冷却保护
                if now < self._blocked_until:
                    sleep_time = self._blocked_until - now
                else:
                    # 补充令牌
                    elapsed = now - self._last_token_time
                    self._last_token_time = now
                    self._tokens = min(self.burst, self._tokens + elapsed * self.requests_per_second)

                    # 尝试消费 1 个令牌
                    if self._tokens >= 1.0:
                        self._tokens -= 1.0
                        return
                    # 令牌不足，计算补齐 1 个令牌所需时间
                    sleep_time = (1.0 - self._tokens) / self.requests_per_second

            if sleep_time > 0:
                time.sleep(sleep_time)

    def _request(self, path):
        url = BASE_URL + path
        last_error = None
        for attempt in range(3):
            self._throttle()
            t0 = time.perf_counter()
            try:
                resp = requests.get(
                    url,
                    headers={**self._headers, "Cookie": self._cookie_header()},
                    timeout=self.timeout,
                )
                duration_ms = (time.perf_counter() - t0) * 1000
            except requests.RequestException as exc:
                duration_ms = (time.perf_counter() - t0) * 1000
                last_error = VRChatAPIError("网络错误: %s" % exc)
                retry_delay = 1.5 * (attempt + 1)
                retry_info = f"，将在 {retry_delay:.1f}s 后重试 ({attempt + 1}/2)" if attempt < 2 else "，重试耗尽"
                _log_api("GET", path, status_text="网络错误", duration_ms=duration_ms, attempt=attempt,
                         detail=f"{exc}{retry_info}")
                time.sleep(retry_delay)
                continue

            if resp.status_code == 200:
                try:
                    data = resp.json()
                    _log_api("GET", path, status_code=200, duration_ms=duration_ms, attempt=attempt)
                    with self._lock:
                        self._consecutive_successes += 1
                        # 连续成功 10 次且速率被降速过时，逐步恢复速率（每次 +0.2 req/s）
                        if self._consecutive_successes >= 10 and self.requests_per_second < self.base_requests_per_second:
                            self.requests_per_second = min(
                                self.base_requests_per_second,
                                self.requests_per_second + 0.2
                            )
                            self._consecutive_successes = 0
                    return data
                except ValueError:
                    last_error = VRChatAPIError("返回的不是 JSON")
                    retry_delay = 1.5 * (attempt + 1)
                    retry_info = f"，将在 {retry_delay:.1f}s 后重试 ({attempt + 1}/2)" if attempt < 2 else "，重试耗尽"
                    _log_api("GET", path, status_code=200, status_text="200 (非 JSON)", duration_ms=duration_ms,
                             attempt=attempt, detail=f"响应无法解析为 JSON{retry_info}")
                    time.sleep(retry_delay)
                    continue

            if resp.status_code == 429:
                retry_after = 5.0
                try:
                    retry_after = max(1.0, float(resp.headers.get("Retry-After", "5")))
                except (TypeError, ValueError):
                    pass
                with self._lock:
                    self.requests_per_second = max(0.5, self.requests_per_second * 0.7)
                    self._consecutive_successes = 0
                    self._tokens = 0.0
                    now = time.monotonic()
                    self._blocked_until = now + retry_after
                    self._last_token_time = self._blocked_until
                retry_info = f"触发限流，退避 {retry_after:.1f}s 后重试 ({attempt + 1}/2)" if attempt < 2 else "触发限流 (429)，重试耗尽"
                _log_api("GET", path, status_code=429, duration_ms=duration_ms, attempt=attempt, detail=retry_info)
                time.sleep(retry_after)
                last_error = VRChatAPIError("触发限流(429)", status=429)
                continue

            if resp.status_code == 401:
                _log_api("GET", path, status_code=401, duration_ms=duration_ms, attempt=attempt, detail="Cookie 无效或已过期")
                raise VRChatAPIError("Cookie 无效或已过期(401)", status=401)

            if resp.status_code == 404:
                _log_api("GET", path, status_code=404, duration_ms=duration_ms, attempt=attempt, detail="用户不存在")
                raise VRChatAPIError("用户不存在(404)", status=404)

            retry_delay = 1.5 * (attempt + 1)
            retry_info = f"，将在 {retry_delay:.1f}s 后重试 ({attempt + 1}/2)" if attempt < 2 else "，重试耗尽"
            _log_api("GET", path, status_code=resp.status_code, duration_ms=duration_ms, attempt=attempt,
                     detail=f"HTTP 异常状态码{retry_info}")
            last_error = VRChatAPIError("HTTP %s" % resp.status_code, status=resp.status_code)
            time.sleep(retry_delay)

        raise last_error or VRChatAPIError("请求失败")

    def get_me(self):
        """验证 cookie；成功返回自己的用户资料 dict。"""
        return self._request("/auth/user")

    def get_user(self, user_id):
        """旧接口：返回精简用户对象（已不含 bio/bioLinks，保留用于身份/状态等字段）。"""
        return self._request("/users/" + user_id)

    def get_profile(self, user_id):
        """新接口：/profile/{userId} 是当前唯一还返回 bio/bioLinks/头像的端点
        （2025-09 API 变更后，/users/{id} 与搜索都只返回精简对象）。"""
        return self._request("/profile/" + user_id)

    def search_users(self, name, n=20):
        """按名字搜索玩家，返回精简用户对象列表（id、displayName、tags、头像等）。"""
        return self._request("/users?search=%s&n=%d" % (urllib.parse.quote(name), int(n)))

    def get_friends(self):
        """当前 Cookie 账号的全部好友（精简对象），自动翻页。"""
        out = []
        offset = 0
        page = 50
        while offset <= 4000:  # 上限保护，防接口异常时死循环
            batch = self._request("/auth/user/friends?offset=%d&n=%d" % (offset, page))
            if not isinstance(batch, list):
                break
            out.extend(batch)
            if len(batch) < page:
                break
            offset += page
        return out

    def get_mutual_counts(self, user_id):
        """与某玩家的共同项计数：{"friendCount": N, "groupCount": M}（隐藏的共同好友也计入）。"""
        return self._request("/users/%s/mutuals" % user_id)
