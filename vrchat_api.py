"""VRChat API 客户端：用 auth / twoFactorAuth cookie 直接调用，带限流与重试。

接口基址 https://api.vrchat.cloud/api/1
  GET /auth/user      验证 cookie，返回自己的资料
  GET /users/{userId} 返回指定用户资料（bio、bioLinks、头像缩略图等）
VRChat 要求自定义 User-Agent（带联系方式），并明确要求控制请求频率，这里默认 2 req/s，
收到 429 时按 Retry-After 退避并自动降低速率。
"""
import threading
import time

import requests

BASE_URL = "https://api.vrchat.cloud/api/1"
DEFAULT_UA = "profile-check/1.0 (personal VRChat profile matcher; contact: local-user)"


class VRChatAPIError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class VRChatAPI:
    def __init__(self, auth_cookie="", two_factor_cookie="", requests_per_second=2.0,
                 user_agent=None, timeout=20):
        self.auth_cookie = (auth_cookie or "").strip()
        self.two_factor_cookie = (two_factor_cookie or "").strip()
        self.requests_per_second = max(0.2, float(requests_per_second))
        self.timeout = timeout
        self._headers = {
            "User-Agent": user_agent or DEFAULT_UA,
            "Accept": "application/json",
        }
        self._lock = threading.Lock()
        self._next_slot = 0.0

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
        with self._lock:
            now = time.monotonic()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + 1.0 / self.requests_per_second
        if wait > 0:
            time.sleep(wait)

    def _request(self, path):
        url = BASE_URL + path
        last_error = None
        for attempt in range(3):
            self._throttle()
            try:
                resp = requests.get(
                    url,
                    headers={**self._headers, "Cookie": self._cookie_header()},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = VRChatAPIError("网络错误: %s" % exc)
                time.sleep(1.5 * (attempt + 1))
                continue

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    last_error = VRChatAPIError("返回的不是 JSON")
                    continue

            if resp.status_code == 429:
                retry_after = 5.0
                try:
                    retry_after = max(1.0, float(resp.headers.get("Retry-After", "5")))
                except (TypeError, ValueError):
                    pass
                with self._lock:
                    self.requests_per_second = max(0.3, self.requests_per_second * 0.7)
                time.sleep(retry_after)
                last_error = VRChatAPIError("触发限流(429)", status=429)
                continue

            if resp.status_code == 401:
                raise VRChatAPIError("Cookie 无效或已过期(401)", status=401)
            if resp.status_code == 404:
                raise VRChatAPIError("用户不存在(404)", status=404)

            last_error = VRChatAPIError("HTTP %s" % resp.status_code, status=resp.status_code)
            time.sleep(1.5 * (attempt + 1))

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
