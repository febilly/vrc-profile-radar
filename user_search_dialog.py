"""通用搜索选人对话框：输入名字 → VRChat API 搜索 → 头像 + 名字 + 信任等级 → 点选回调。

可复用：UserSearchDialog(parent, get_api, on_pick) —— on_pick(user_dict) 会在主线程被调用，
user_dict 为 API 返回的精简用户对象（含 id / displayName / tags 等）。用于排除名单等场景。
"""
import io
import queue
import threading
import tkinter as tk
from tkinter import ttk

import requests

import theme
from theme import BG, BG_INPUT, BORDER, FG, FG_DIM, ERR, WARN
from trust import user_trust

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

AVATAR = 36
MAX_RESULTS = 20
_AVATAR_FALLBACK = (
    "currentAvatarThumbnailImageUrl", "profilePicOverride", "userIcon",
    "currentAvatarImageUrl", "iconUrl",
)


def _avatar_url(user):
    for key in _AVATAR_FALLBACK:
        if user.get(key):
            return user[key]
    return ""


def _trust_of(user):
    return user_trust(user.get("tags") or user.get("trustTags"), user.get("developerType", ""))


class UserSearchDialog(tk.Toplevel):
    def __init__(self, parent, get_api, on_pick, title="搜索玩家"):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=BG)
        self.get_api = get_api
        self.on_pick = on_pick
        self._events = queue.Queue()
        self._rows = []           # {"user", "frame", "avatar_label"}
        self._photos = {}

        # 本程序的 root 是 withdrawn 的，不能用 transient；用 grab_set 保持模态
        self.geometry("%dx%d" % (theme.px(420), theme.px(480)))
        self.bind("<Escape>", lambda e: self.destroy())

        bar = ttk.Frame(self, padding=(10, 10))
        bar.pack(fill="x")
        self.query_var = tk.StringVar()
        self.query_entry = ttk.Entry(bar, textvariable=self.query_var)
        self.query_entry.pack(side="left", fill="x", expand=True)
        self.query_entry.bind("<Return>", lambda e: self._do_search())
        ttk.Button(bar, text="搜索", command=self._do_search, style="Tool.TButton",
                   width=6).pack(side="left", padx=(8, 0))

        self.status_label = ttk.Label(self, text="输入玩家名字后回车搜索", style="Dim.TLabel",
                                      padding=(10, 2))
        self.status_label.pack(fill="x")

        body = ttk.Frame(self, padding=(10, 0))
        body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(body, highlightthickness=0, bg=BG)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw", tags="inner")
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            "inner", width=e.width))
        self.canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(
            -e.delta // 120, "units"))

        tip = ttk.Label(self, text="点击结果行加入名单", style="Dim.TLabel", padding=(10, 6))
        tip.pack(fill="x", side="bottom")

        self.query_entry.focus_set()
        self.after(120, self._poll)
        self.grab_set()

    # ---- 搜索 ----

    def _do_search(self):
        name = self.query_var.get().strip()
        if not name:
            return
        api = self.get_api()
        if api is None or not api.configured:
            self._set_status("请先在设置里配置 VRChat Cookie 再搜索", WARN)
            return
        self._set_status("搜索中…", FG_DIM)
        self._clear_rows()

        def work():
            try:
                users = api.search_users(name)
                self._events.put(("results", users if isinstance(users, list) else []))
            except Exception as exc:
                self._events.put(("error", str(exc)))

        threading.Thread(target=work, daemon=True, name="UserSearch").start()

    def _set_status(self, text, color=FG_DIM):
        self.status_label.config(text=text, foreground=color)

    # ---- 结果渲染 ----

    def _poll(self):
        try:
            for _ in range(50):
                self._handle(*self._events.get_nowait())
        except queue.Empty:
            pass
        self.after(120, self._poll)

    def _handle(self, kind, *payload):
        if kind == "results":
            users = payload[0]
            if not users:
                self._set_status("没有找到匹配的玩家", WARN)
                return
            self._set_status("找到 %d 个结果，点击选择" % len(users))
            self._render(users)
        elif kind == "error":
            self._set_status("搜索失败：%s" % payload[0], ERR)
        elif kind == "avatar":
            idx, data = payload
            self._apply_avatar(idx, data)

    def _clear_rows(self):
        for child in self.inner.winfo_children():
            child.destroy()
        self._rows.clear()

    def _render(self, users):
        self._clear_rows()
        for idx, user in enumerate(users[:MAX_RESULTS]):
            name = user.get("displayName") or "(未知)"
            trust_name, trust_color, _ = _trust_of(user)

            row = tk.Frame(self.inner, bg=BG)
            row.pack(fill="x", padx=2, pady=3)
            avatar = tk.Label(row, bg="#e8e8e8", fg=FG_DIM, text="·", font=(theme.FONT, 9))
            if not _avatar_url(user):
                avatar.config(text="👤", font=("Segoe UI Emoji", 12))
            avatar.pack(side="left", padx=(0, 8))
            info = tk.Frame(row, bg=BG)
            info.pack(side="left", fill="x", expand=True)
            tk.Label(info, text=name, bg=BG, fg=FG,
                     font=(theme.FONT, 10, "bold"), anchor="w").pack(anchor="w")
            tk.Label(info, text=user.get("id", ""), bg=BG, fg=FG_DIM,
                     font=(theme.FONT, 8), anchor="w").pack(anchor="w")
            tk.Label(row, text=trust_name, bg=BG, fg=trust_color,
                     font=(theme.FONT, 9)).pack(side="right", padx=(0, 4))

            widgets = [row] + row.winfo_children() + info.winfo_children()
            for w in widgets:
                w.bind("<Button-1>", lambda e, u=user: self._pick(u))
                w.bind("<Enter>", lambda e, r=row: r.config(bg=theme.BG_HOVER))
                w.bind("<Leave>", lambda e, r=row: r.config(bg=BG))
                w.config(cursor="hand2")
                for child in (w.winfo_children() if isinstance(w, tk.Frame) else []):
                    try:
                        child.config(cursor="hand2")
                    except tk.TclError:
                        pass
            self._rows.append({"user": user, "avatar_label": avatar})

        if HAS_PIL:
            threading.Thread(target=self._fetch_avatars, daemon=True,
                             name="SearchAvatars").start()

    def _pick(self, user):
        self.on_pick(user)
        self.destroy()

    def _fetch_avatars(self):
        api = self.get_api() if callable(self.get_api) else self.get_api
        headers = {}
        if api and hasattr(api, "_headers"):
            headers["User-Agent"] = api._headers.get("User-Agent") or "profile-check/1.0"
        else:
            headers["User-Agent"] = "profile-check/1.0 (personal VRChat profile matcher; contact: local-user)"
        cookies = {}
        if api and getattr(api, "auth_cookie", None):
            cookies["auth"] = api.auth_cookie
            if getattr(api, "two_factor_cookie", None):
                cookies["twoFactorAuth"] = api.two_factor_cookie

        for idx, row in enumerate(self._rows):
            url = _avatar_url(row["user"])
            if not url:
                continue
            try:
                resp = requests.get(url, headers=headers, cookies=cookies, timeout=15)
                if resp.status_code == 200 and resp.content:
                    self._events.put(("avatar", idx, resp.content))
            except Exception:
                continue

    def _apply_avatar(self, idx, data):
        if not HAS_PIL:
            return
        if idx >= len(self._rows):
            return
        try:
            img = Image.open(io.BytesIO(data)).convert("RGBA").resize(
                (theme.px(AVATAR), theme.px(AVATAR)), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img, master=self)
        except Exception:
            return
        self._photos[idx] = photo
        row = self._rows[idx]
        label = row["avatar_label"]
        label.config(image=photo, text="", width=theme.px(AVATAR), height=theme.px(AVATAR))
