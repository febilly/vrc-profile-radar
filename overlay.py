"""悬浮窗 HUD：无边框深色小窗，自动随内容缩放，显示匹配到的玩家。

- 标题栏可拖动、可折叠；右键菜单：主面板 / 折叠 / 置顶 / 透明度 / 退出
- 条目：小头像 + 昵称 + 命中规则彩色标签；悬停看理由，单击看详情
- 窗口大小随匹配人数自动收缩/展开，不占地方
"""
import ctypes
import io
import queue
import tkinter as tk

import theme
from theme import BG, BG_CARD, BG_HOVER, BORDER, FG, FG_DIM, OK, WARN, ERR

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

AVATAR_BASE = 22     # 96DPI 基准像素，实际用 theme.px() 缩放
MIN_WIDTH_BASE = 200
MAX_WIDTH_FACTOR = 0.4   # 悬浮窗最大宽度占屏幕比例
NAME_FONT = (theme.FONT, 10)
SMALL_FONT = (theme.FONT, 9)
TINY_FONT = (theme.FONT, 8)
STATUS_COLOR = {"info": OK, "warn": WARN, "error": ERR}


def short_text(text, limit):
    return text if len(text) <= limit else text[:limit] + "…"


class Overlay:
    def __init__(self, root, config, matcher, open_main, on_quit):
        self.root = root
        self.config = config
        self.matcher = matcher
        self.open_main = open_main
        self.on_quit = on_quit

        self.events = queue.Queue()
        matcher.sinks.append(self.events.put)

        self.matches = []
        self.photos = {}          # uid -> PhotoImage
        self.row_avatar_labels = {}
        self.status_text = ""
        self.status_level = "info"
        self.collapsed = False
        self._drag = None
        self.avatar_px = theme.px(AVATAR_BASE)

        self.top = tk.Toplevel(root)
        self.top.title("overlay")
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", bool(config.get("ui", "always_on_top", default=True)))
        self._alpha = float(config.get("ui", "alpha", default=0.9) or 0.9)
        self.top.configure(bg=BG, highlightthickness=1,
                           highlightbackground=BORDER, highlightcolor=BORDER)

        self._build_header()
        self.body = tk.Frame(self.top, bg=BG)
        self.body.pack(fill="x", padx=1, pady=(0, 1))
        self._rebuild_rows()

        self.set_alpha(self._alpha)
        self._bind_drag()
        self._restore_position()
        self.top.after(150, self._poll)

    # ---------- 透明度 ----------

    def set_alpha(self, value):
        """统一入口：Tk 属性 + Win32 兜底，保证透明度一定生效。"""
        value = min(1.0, max(0.05, float(value)))
        self._alpha = value
        try:
            self.top.attributes("-alpha", value)
        except tk.TclError:
            pass
        self._win32_apply_alpha(value)

    def _win32_apply_alpha(self, value):
        """兜底：部分 Tk 构建对 overrideredirect 窗口可能不真正应用 -alpha，
        直接补上 WS_EX_LAYERED 并写入不透明度。"""
        if not hasattr(ctypes, "windll"):
            return
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.top.winfo_id())
            if not hwnd:
                return
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            LWA_ALPHA = 2
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if not style & WS_EX_LAYERED:
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
            user32.SetLayeredWindowAttributes(hwnd, 0, int(value * 255), LWA_ALPHA)
        except Exception:
            pass

    # ---------- 布局 ----------

    def _build_header(self):
        self.header = tk.Frame(self.top, bg=BG_CARD)
        self.header.pack(fill="x", padx=1, pady=(1, 0))

        # 右侧小按钮先 pack，避免长副标题把它们挤出窗口
        collapse = tk.Label(self.header, text="–", bg=BG_CARD, fg=FG_DIM, font=(theme.FONT, 10, "bold"),
                            cursor="hand2", width=2)
        collapse.pack(side="right", padx=(0, 4))
        collapse.bind("<Button-1>", lambda e: self.toggle_collapse())
        gear = tk.Label(self.header, text="⚙", bg=BG_CARD, fg=FG_DIM, font=("Segoe UI Symbol", 10),
                        cursor="hand2", width=2)
        gear.pack(side="right")
        gear.bind("<Button-1>", lambda e: self.open_main())

        self._dot_size = theme.px(8)
        self.dot = tk.Canvas(self.header, width=self._dot_size, height=self._dot_size,
                             bg=BG_CARD, highlightthickness=0)
        self.dot.pack(side="left", padx=(8, 5))
        self._draw_dot("#888888")

        self.title_label = tk.Label(self.header, text="匹配 0", bg=BG_CARD, fg=FG,
                                    font=(theme.FONT, 10, "bold"))
        self.title_label.pack(side="left")

        self.sub_label = tk.Label(self.header, text="", bg=BG_CARD, fg=FG_DIM, font=TINY_FONT)
        self.sub_label.pack(side="left", padx=(6, 0))

        for w in (self.header, self.dot, self.title_label, self.sub_label):
            w.bind("<Button-3>", self._popup_menu)

    def _rebuild_rows(self):
        for child in self.body.winfo_children():
            child.destroy()
        self.row_avatar_labels.clear()
        show_avatars = bool(self.config.get("ui", "show_avatars", default=True))

        if not self.matches:
            hint = tk.Label(self.body, text="暂无匹配 · 右键打开设置", bg=BG, fg=FG_DIM, font=TINY_FONT,
                            anchor="w", padx=10, pady=4)
            hint.pack(fill="x")
        else:
            for match in self.matches:
                self._build_row(match, show_avatars)
        self._refit()

    def _build_row(self, match, show_avatars):
        row = tk.Frame(self.body, bg=BG)
        row.pack(fill="x", padx=6, pady=2)

        uid = match.get("user_id")
        avatar = None
        if show_avatars:
            avatar = tk.Label(row, bg=BG, fg=FG_DIM, font=(theme.FONT, 8))
            if uid in self.photos:
                photo = self.photos[uid]
                avatar.config(image=photo, text="", width=self.avatar_px, height=self.avatar_px)
            else:
                avatar.config(text="·", width=2, height=1)
            avatar.pack(side="left", padx=(0, 6))
            if uid:
                self.row_avatar_labels[uid] = avatar

        name = tk.Label(row, text=match.get("name") or "?", bg=BG, fg=FG, font=NAME_FONT)
        name.pack(side="left")
        for rule in match.get("rules", [])[:3]:
            chip = tk.Label(row, text=short_text(rule.get("name", ""), 10), bg=theme.CHIP_BG,
                            fg=rule.get("color") or theme.chip_color(rule.get("name", "")),
                            font=TINY_FONT, padx=5, pady=1)
            chip.pack(side="left", padx=(5, 0))
        more = len(match.get("rules", [])) - 3
        if more > 0:
            tk.Label(row, text="+%d" % more, bg=BG, fg=FG_DIM, font=TINY_FONT).pack(side="left", padx=(4, 0))

        widgets = [row] + row.winfo_children()
        reasons = "；".join("%s: %s" % (r.get("name", ""), r.get("reason", ""))
                            for r in match.get("rules", []) if r.get("reason"))
        for w in widgets:
            w.bind("<Button-1>", lambda e, m=match: self._show_detail(m))
            w.bind("<Enter>", lambda e, r=row: self._hover(r, True))
            w.bind("<Leave>", lambda e, r=row: self._hover(r, False))
            if reasons:
                self._tooltip(w, reasons)

    def _hover(self, row, on):
        color = BG_HOVER if on else BG
        row.config(bg=color)
        for child in row.winfo_children():
            try:
                if not child.cget("image"):
                    child.config(bg=color)
            except tk.TclError:
                pass

    def _tooltip(self, widget, text):
        tip = {"win": None}

        def enter(_):
            if tip["win"] or not text:
                return
            x = widget.winfo_rootx() + 12
            y = widget.winfo_rooty() + widget.winfo_height() + 3
            tw = tk.Toplevel(widget)
            tw.wm_overrideredirect(True)
            tw.wm_geometry("+%d+%d" % (x, y))
            tk.Label(tw, text=text, background="#0f1011", foreground=FG,
                     relief="flat", font=TINY_FONT, justify="left",
                     padx=6, pady=3).pack()
            tip["win"] = tw

        def leave(_):
            if tip["win"]:
                tip["win"].destroy()
                tip["win"] = None

        widget.bind("<Enter>", enter, add="+")
        widget.bind("<Leave>", leave, add="+")

    # ---------- 尺寸/位置 ----------

    def _refit(self):
        self.top.update_idletasks()
        w = max(self.top.winfo_reqwidth(), theme.px(MIN_WIDTH_BASE))
        w = min(w, int(self.top.winfo_screenwidth() * MAX_WIDTH_FACTOR))
        h = self.top.winfo_reqheight()
        if self._pos_saved():
            x, y = self._saved_pos()
            x = max(0, min(x, self.top.winfo_screenwidth() - w))
        else:
            # 未保存过位置时始终贴屏幕右上角（窗口变宽时向左生长，不会跑出屏幕）
            x = self.top.winfo_screenwidth() - w - theme.px(48)
            y = 120
        self.top.geometry("%dx%d+%d+%d" % (w, h, x, y))

    def _pos_saved(self):
        return self.config.get("ui", "overlay_x", default=-1) >= 0

    def _saved_pos(self):
        sw, sh = self.top.winfo_screenwidth(), self.top.winfo_screenheight()
        x = min(max(0, self.config.get("ui", "overlay_x", default=40)), sw - 80)
        y = min(max(0, self.config.get("ui", "overlay_y", default=120)), sh - 60)
        return x, y

    def _restore_position(self):
        # 初始位置由 _refit 决定（未保存过时贴屏幕右上角）
        pass

    def _bind_drag(self):
        def start(e):
            self._drag = (e.x_root - self.top.winfo_x(), e.y_root - self.top.winfo_y())

        def move(e):
            if self._drag:
                self.top.geometry("+%d+%d" % (e.x_root - self._drag[0], e.y_root - self._drag[1]))

        def release(_):
            if self._drag:
                self._drag = None
                self.config.set(self.top.winfo_x(), "ui", "overlay_x")
                self.config.set(self.top.winfo_y(), "ui", "overlay_y")

        for w in (self.header, self.dot, self.title_label, self.sub_label):
            w.bind("<Button-1>", start, add="+")
            w.bind("<B1-Motion>", move, add="+")
            w.bind("<ButtonRelease-1>", release, add="+")

    def toggle_collapse(self):
        self.collapsed = not self.collapsed
        if self.collapsed:
            self.body.pack_forget()
        else:
            self.body.pack(fill="x", padx=1, pady=(0, 1))
        self._refit()

    # ---------- 菜单 ----------

    def _popup_menu(self, event):
        menu = tk.Menu(self.top, tearoff=0, bg="#0f1011", fg=FG,
                       activebackground=BG_HOVER, activeforeground=FG,
                       font=SMALL_FONT, bd=0)
        menu.add_command(label="打开主面板", command=self.open_main)
        menu.add_command(label="折叠" if not self.collapsed else "展开",
                         command=self.toggle_collapse)
        menu.add_separator()
        topmost = tk.BooleanVar(value=bool(self.config.get("ui", "always_on_top", default=True)))
        menu.add_checkbutton(label="窗口置顶", variable=topmost,
                             command=lambda: self._set_topmost(topmost.get()))
        alpha_menu = tk.Menu(menu, tearoff=0, bg="#0f1011", fg=FG,
                             activebackground=BG_HOVER, font=SMALL_FONT)
        for pct in (100, 90, 80, 70):
            alpha_menu.add_command(label="%d%%" % pct,
                                   command=lambda p=pct: self._set_alpha(p / 100.0))
        menu.add_cascade(label="透明度", menu=alpha_menu)
        menu.add_separator()
        menu.add_command(label="退出", command=self.on_quit)
        menu.tk_popup(event.x_root, event.y_root)

    def _set_topmost(self, value):
        self.top.attributes("-topmost", bool(value))
        self.config.set(bool(value), "ui", "always_on_top")

    def _set_alpha(self, value):
        self.set_alpha(value)
        self.config.set(value, "ui", "alpha")

    # ---------- 事件 ----------

    def _poll(self):
        try:
            for _ in range(50):
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.top.after(150, self._poll)

    def _handle(self, event):
        etype = event.get("type")
        if etype == "status":
            if event.get("text"):
                self.status_text = event["text"]
            self.status_level = event.get("level", "info")
            if event.get("players") is not None:
                self._players = event["players"]
        elif etype == "matches":
            self.matches = event.get("matches", [])
            if event.get("players") is not None:
                self._players = event["players"]
        elif etype == "avatar":
            self._apply_avatar(event.get("user_id"), event.get("data"))
            return
        self._schedule_refresh()

    def _schedule_refresh(self):
        """合并短时间内的多次事件，避免扫描时频繁重建闪烁。"""
        if getattr(self, "_refresh_pending", False):
            return
        self._refresh_pending = True
        self.top.after(80, self._do_refresh)

    def _do_refresh(self):
        self._refresh_pending = False
        self._refresh_header()
        self._rebuild_rows()

    def _refresh_header(self):
        self._draw_dot(STATUS_COLOR.get(self.status_level, "#888888"))
        self.title_label.config(text="匹配 %d" % len(self.matches))
        parts = []
        players = getattr(self, "_players", 0)
        if players:
            parts.append("房间 %d 人" % players)
        if self.status_text:
            parts.append(short_text(self.status_text, 14))
        self.sub_label.config(text=" · ".join(parts))

    def _draw_dot(self, color):
        self.dot.delete("all")
        d = self._dot_size - 1
        self.dot.create_oval(0, 0, d, d, fill=color, outline="")

    def _apply_avatar(self, uid, data):
        if not uid or not data or not HAS_PIL:
            return
        try:
            size = self.avatar_px
            img = Image.open(io.BytesIO(data)).convert("RGBA").resize((size, size), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img, master=self.root)
        except Exception:
            return
        self.photos[uid] = photo
        label = self.row_avatar_labels.get(uid)
        if label:
            label.config(image=photo, text="", width=self.avatar_px, height=self.avatar_px)

    def apply_settings(self):
        self.top.attributes("-topmost", bool(self.config.get("ui", "always_on_top", default=True)))
        self.set_alpha(float(self.config.get("ui", "alpha", default=0.9) or 0.9))
        self._rebuild_rows()

    # ---------- 详情弹窗 ----------

    def _show_detail(self, match):
        import webbrowser
        win = tk.Toplevel(self.top)
        win.title(match.get("name") or "详情")
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        body = tk.Frame(win, bg=BG, padx=12, pady=10)
        body.pack(fill="both", expand=True)

        head = tk.Frame(body, bg=BG)
        head.pack(fill="x")
        if HAS_PIL and self.photos.get(match.get("user_id")):
            tk.Label(head, image=self.photos[match.get("user_id")], bg=BG).pack(side="left", padx=(0, 10))
        tk.Label(head, text=match.get("name") or "", bg=BG, fg=FG,
                 font=(theme.FONT, 13, "bold")).pack(anchor="w")
        if match.get("user_id"):
            tk.Label(head, text=match["user_id"], bg=BG, fg=FG_DIM, font=TINY_FONT).pack(anchor="w")

        for rule in match.get("rules", []):
            line = tk.Frame(body, bg=BG)
            line.pack(fill="x", pady=(8, 0))
            tk.Label(line, text=rule.get("name", ""), bg=theme.CHIP_BG,
                     fg=rule.get("color") or theme.chip_color(rule.get("name", "")),
                     font=SMALL_FONT, padx=6, pady=1).pack(side="left")
            if rule.get("reason"):
                tk.Label(line, text="  " + rule["reason"], bg=BG, fg=FG_DIM,
                         font=SMALL_FONT).pack(side="left")

        tk.Label(body, text="简介", bg=BG, fg=FG_DIM, font=SMALL_FONT).pack(anchor="w", pady=(12, 2))
        bio = tk.Text(body, height=8, wrap="word", relief="flat", bg="#131417", fg=FG,
                      font=SMALL_FONT, padx=8, pady=6, highlightthickness=1,
                      highlightbackground=BORDER)
        bio.insert("1.0", (match.get("profile") or {}).get("bio") or "(无简介)")
        bio.config(state="disabled")
        bio.pack(fill="both", expand=True)

        links = (match.get("profile") or {}).get("bioLinks") or []
        for link in links:
            lbl = tk.Label(body, text=link, bg=BG, fg="#6cb2ff", cursor="hand2",
                           font=SMALL_FONT, anchor="w")
            lbl.pack(anchor="w", pady=(2, 0))
            lbl.bind("<Button-1>", lambda e, u=link: webbrowser.open(u))
