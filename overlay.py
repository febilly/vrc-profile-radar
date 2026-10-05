"""悬浮窗 HUD：无边框深色小窗，自动随内容缩放，显示匹配到的玩家。

- 标题栏可拖动、可折叠；右键菜单：主面板 / 折叠 / 置顶 / 透明度 / 退出
- 条目：小头像 + 昵称 + 命中规则彩色标签；悬停看理由，单击看详情
- 窗口大小随匹配人数自动收缩/展开，不占地方
"""
import ctypes
import hashlib
import io
import os
import queue
import threading
import time
import tkinter as tk
import webbrowser

import theme
from theme import BG, BG_CARD, BG_HOVER, BORDER, FG, FG_DIM, OK, WARN, ERR

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

AVATAR_BASE = 22     # 96DPI 基准像素，实际用 theme.px() 缩放
MIN_WIDTH_BASE = 200
MAX_WIDTH_FACTOR = 0.95   # 悬浮窗最大宽度占屏幕比例（支持多列）
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
        self.ui_queue = queue.Queue()

        self.matches = []
        self.photos = {}          # uid -> PhotoImage
        self.row_avatar_labels = {}
        self.status_text = ""
        self.status_level = "info"
        self.collapsed = False
        self._drag = None
        self.avatar_px = theme.px(AVATAR_BASE)

        self.top = tk.Toplevel(root)
        title = "VRChat Profile 雷达（悬浮窗）"
        if getattr(root, "_demo", False):
            title += "（演示模式）"
        self.top.title(title)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", bool(config.get("ui", "always_on_top", default=True)))
        self._alpha = float(config.get("ui", "alpha", default=0.9) or 0.9)
        self.top.configure(bg=BG, highlightthickness=1,
                           highlightbackground=BORDER, highlightcolor=BORDER)

        self._build_header()
        self.body = tk.Frame(self.top, bg=BG)
        self.body.pack(fill="both", expand=True, padx=1, pady=(0, 1))
        self.content_frame = None
        self._rendered_matches_sig = None
        self._header_refresh_pending = False
        self._full_refresh_pending = False
        self._rebuild_rows()

        self.set_alpha(self._alpha)
        self._bind_drag()
        self._restore_position()
        self._win32_set_appwindow()
        self.top.after(10, self._win32_set_appwindow)
        self.top.after(150, self._poll)

    # ---------- 透明度与任务栏窗口 ----------

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
            hwnd = user32.GetParent(self.top.winfo_id()) or self.top.winfo_id()
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

    def _win32_set_appwindow(self):
        """让无边框悬浮窗在 Windows 任务栏中作为一个独立应用窗口可见，支持任务栏点击最小化与恢复。"""
        if not hasattr(ctypes, "windll"):
            return
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.top.winfo_id()) or self.top.winfo_id()
            if not hwnd:
                return
            GWL_STYLE = -16
            GWL_EXSTYLE = -20
            WS_EX_APPWINDOW = 0x00040000
            WS_EX_TOOLWINDOW = 0x00000080
            WS_MINIMIZEBOX = 0x00020000
            WS_SYSMENU = 0x00080000

            # 启用最小化能力与系统菜单（允许任务栏点击最小化/还原）
            style = user32.GetWindowLongW(hwnd, GWL_STYLE)
            user32.SetWindowLongW(hwnd, GWL_STYLE, style | WS_MINIMIZEBOX | WS_SYSMENU)

            # 移除 WS_EX_TOOLWINDOW，加入 WS_EX_APPWINDOW
            ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, (ex_style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW)

            self.top.withdraw()
            self.top.deiconify()
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

    def _matches_signature(self, matches):
        sig = []
        for m in (matches or []):
            uid = m.get("user_id") or ""
            name = m.get("name") or ""
            rules = tuple(
                (r.get("id"), r.get("name"), r.get("color"), r.get("reason"))
                for r in m.get("rules", [])
            )
            sig.append((uid, name, rules))
        return tuple(sig)

    def _matches_changed(self, new_matches):
        return self._matches_signature(self.matches) != self._matches_signature(new_matches)

    def _rebuild_rows(self, force=False):
        show_avatars = bool(self.config.get("ui", "show_avatars", default=True))
        try:
            max_rows = int(self.config.get("ui", "overlay_max_rows", default=20) or 20)
        except (ValueError, TypeError):
            max_rows = 20
        if max_rows < 1:
            max_rows = 20

        sig = (self._matches_signature(self.matches), show_avatars, max_rows)
        if not force and sig == self._rendered_matches_sig:
            return
        self._rendered_matches_sig = sig

        new_frame = tk.Frame(self.body, bg=BG)
        new_avatar_labels = {}

        if not self.matches:
            hint = tk.Label(new_frame, text="暂无匹配 · 右键打开设置", bg=BG, fg=FG_DIM, font=TINY_FONT,
                            anchor="w", padx=10, pady=4)
            hint.pack(fill="x")
        else:
            # 超过 max_rows 时切分成多列
            columns = [self.matches[i:i + max_rows] for i in range(0, len(self.matches), max_rows)]
            cols_container = tk.Frame(new_frame, bg=BG)
            cols_container.pack(fill="both", expand=True)

            for col_idx, col_matches in enumerate(columns):
                if col_idx > 0:
                    sep = tk.Frame(cols_container, bg=BORDER, width=1)
                    sep.pack(side="left", fill="y", padx=3, pady=2)
                col_frame = tk.Frame(cols_container, bg=BG)
                col_frame.pack(side="left", fill="y", anchor="n")
                for match in col_matches:
                    self._build_row(col_frame, match, show_avatars, new_avatar_labels)

        new_frame.pack(fill="both", expand=True)
        old_frame = self.content_frame
        self.content_frame = new_frame
        self.row_avatar_labels = new_avatar_labels
        if old_frame is not None:
            old_frame.destroy()
        self._refit()

    def _build_row(self, parent, match, show_avatars, avatar_dict=None):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", padx=6, pady=2)

        uid = match.get("user_id")
        avatar = None
        if show_avatars:
            avatar = tk.Label(row, bg=BG, fg=FG_DIM, font=(theme.FONT, 8))
            if uid and uid not in self.photos and HAS_PIL and hasattr(self.matcher, "avatar_dir"):
                disk_path = os.path.join(self.matcher.avatar_dir, hashlib.md5(uid.encode()).hexdigest() + ".png")
                if os.path.exists(disk_path):
                    try:
                        size = self.avatar_px
                        img = Image.open(disk_path).convert("RGBA").resize((size, size), Image.LANCZOS)
                        self.photos[uid] = ImageTk.PhotoImage(img, master=self.root)
                    except Exception:
                        pass

            if uid in self.photos:
                photo = self.photos[uid]
                avatar.config(image=photo, text="", width=self.avatar_px, height=self.avatar_px)
            else:
                avatar.config(text="·", width=2, height=1)
            avatar.pack(side="left", padx=(0, 6))
            if uid:
                if avatar_dict is not None:
                    avatar_dict[uid] = avatar
                else:
                    self.row_avatar_labels[uid] = avatar

        name = tk.Label(row, text=match.get("name") or "?", bg=BG, fg=FG, font=NAME_FONT)
        name.pack(side="left")
        for rule in match.get("rules", [])[:3]:
            fg_color = rule.get("color") or theme.chip_color(rule.get("name", ""))
            chip = tk.Label(row, text=short_text(rule.get("name", ""), 10), bg=theme.CHIP_BG,
                            fg=fg_color, font=TINY_FONT, padx=5, pady=1, cursor="hand2")
            chip._is_chip = True
            chip.pack(side="left", padx=(5, 0))
            chip.bind("<Button-1>", lambda e, r=rule, m=match: self._on_chip_click(e, r, m))
            chip.bind("<Enter>", lambda e, c=chip: c.config(bg="#3d4046"), add="+")
            chip.bind("<Leave>", lambda e, c=chip: c.config(bg=theme.CHIP_BG), add="+")
            chip_reason = rule.get("reason")
            if chip_reason:
                self._tooltip(chip, "%s: %s" % (rule.get("name", ""), chip_reason))
        more = len(match.get("rules", [])) - 3
        if more > 0:
            more_lbl = tk.Label(row, text="+%d" % more, bg=BG, fg=FG_DIM, font=TINY_FONT)
            more_lbl.pack(side="left", padx=(4, 0))

        row_widgets = [w for w in [row] + row.winfo_children() if not getattr(w, "_is_chip", False)]
        reasons = "；".join("%s: %s" % (r.get("name", ""), r.get("reason", ""))
                            for r in match.get("rules", []) if r.get("reason"))
        for w in row_widgets:
            w.bind("<Button-1>", lambda e, m=match: self._show_detail(m))
            w.bind("<Enter>", lambda e, r=row: self._hover(r, True))
            w.bind("<Leave>", lambda e, r=row: self._hover(r, False))
            if reasons:
                self._tooltip(w, reasons)

    def _on_chip_click(self, event, rule, match):
        """点击标签：执行各规则的自定义动作（内置规则为详情弹窗，插件规则为自定义动作如打开网页）。"""
        if self.matcher and hasattr(self.matcher, "plugin_manager"):
            self.matcher.plugin_manager.handle_chip_action(rule, match, on_show_detail=self._show_detail)
        else:
            action = rule.get("action") or {}
            if action.get("type") == "open_url" and action.get("url"):
                import webbrowser
                webbrowser.open(action["url"])
            else:
                self._show_detail(match)
        return "break"

    def _hover(self, row, on):
        color = BG_HOVER if on else BG
        row.config(bg=color)
        for child in row.winfo_children():
            try:
                if not child.cget("image") and not getattr(child, "_is_chip", False):
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
        req_w = self.top.winfo_reqwidth()
        req_h = self.top.winfo_reqheight()
        sw = self.top.winfo_screenwidth()
        sh = self.top.winfo_screenheight()

        min_w = theme.px(MIN_WIDTH_BASE)
        max_w = int(sw * MAX_WIDTH_FACTOR)
        w = max(min(req_w, max_w), min_w)

        # 悬浮窗最大高度避免溢出屏幕下边缘
        max_h = max(100, sh - 40)
        h = min(req_h, max_h)

        if self._pos_saved():
            x, y = self._saved_pos()
            x = max(0, min(x, sw - w))
            y = max(0, min(y, sh - h))
        else:
            # 未保存过位置时始终贴屏幕右上角（窗口变宽时向左生长，不会跑出屏幕）
            x = max(0, sw - w - theme.px(48))
            y = min(120, max(0, sh - h))
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
            self.body.pack(fill="both", expand=True, padx=1, pady=(0, 1))
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
                             activebackground=BG_HOVER, activeforeground=FG, font=SMALL_FONT)
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

    # ---------- 事件与线程安全调度 ----------

    def dispatch_ui(self, callback):
        """线程安全地向 Tkinter 主线程派发 UI 更新任务。"""
        self.ui_queue.put(callback)
        try:
            self.top.after_idle(self._drain_ui)
        except Exception:
            pass

    def _drain_ui(self):
        try:
            for _ in range(50):
                cb = self.ui_queue.get_nowait()
                try:
                    cb()
                except Exception:
                    pass
        except queue.Empty:
            pass

    def _poll(self):
        self._drain_ui()
        try:
            for _ in range(50):
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.top.after(150, self._poll)

    def _handle(self, event):
        etype = event.get("type")
        if etype == "status":
            changed = False
            if event.get("text") and event["text"] != self.status_text:
                self.status_text = event["text"]
                changed = True
            level = event.get("level", "info")
            if level != self.status_level:
                self.status_level = level
                changed = True
            players = event.get("players")
            if players is not None and players != getattr(self, "_players", None):
                self._players = players
                changed = True
            if changed:
                self._schedule_header_refresh()
        elif etype == "matches":
            new_matches = event.get("matches", [])
            players = event.get("players")
            if players is not None and players != getattr(self, "_players", None):
                self._players = players
            changed = self._matches_changed(new_matches)
            self.matches = new_matches
            if changed:
                self._schedule_full_refresh()
            else:
                self._schedule_header_refresh()
        elif etype == "avatar":
            self._apply_avatar(event.get("user_id"), event.get("data"))

    def _schedule_header_refresh(self):
        if getattr(self, "_header_refresh_pending", False) or getattr(self, "_full_refresh_pending", False):
            return
        self._header_refresh_pending = True
        self.top.after(50, self._do_header_refresh)

    def _do_header_refresh(self):
        self._header_refresh_pending = False
        self._refresh_header()

    def _schedule_full_refresh(self):
        if getattr(self, "_full_refresh_pending", False):
            return
        self._full_refresh_pending = True
        self.top.after(50, self._do_full_refresh)

    def _do_full_refresh(self):
        self._full_refresh_pending = False
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
        self._rebuild_rows(force=True)

    # ---------- 详情弹窗 ----------

    def _show_detail(self, match):
        win = tk.Toplevel(self.top)
        self._detail_win = win
        win.title(match.get("name") or "详情")
        win.configure(bg=BG)
        win.attributes("-topmost", True)

        def make_copy_btn(parent, text_to_copy, btn_text="复制"):
            btn = tk.Label(parent, text=btn_text, bg=BG_CARD, fg=FG_DIM,
                           font=TINY_FONT, padx=6, pady=1, cursor="hand2",
                           highlightthickness=1, highlightbackground=BORDER)

            def on_enter(e):
                if btn.cget("text") != "已复制":
                    btn.config(bg=BG_HOVER, fg=FG)

            def on_leave(e):
                if btn.cget("text") != "已复制":
                    btn.config(bg=BG_CARD, fg=FG_DIM)

            def on_click(e):
                if not text_to_copy:
                    return
                try:
                    win.clipboard_clear()
                    win.clipboard_append(text_to_copy)
                    win.update()
                except Exception:
                    pass
                btn.config(text="已复制", fg=OK, bg=BG_CARD)
                win.after(1200, lambda: btn.config(text=btn_text, fg=FG_DIM, bg=BG_CARD))

            btn.bind("<Enter>", on_enter)
            btn.bind("<Leave>", on_leave)
            btn.bind("<Button-1>", on_click)
            return btn

        body = tk.Frame(win, bg=BG, padx=14, pady=12)
        body.pack(fill="both", expand=True)

        head = tk.Frame(body, bg=BG)
        head.pack(fill="x")
        uid = match.get("user_id") or ""

        detail_photo = None
        if HAS_PIL and uid:
            disk_path = os.path.join(self.matcher.avatar_dir, hashlib.md5(uid.encode()).hexdigest() + ".png")
            if os.path.exists(disk_path):
                try:
                    img = Image.open(disk_path).convert("RGBA").resize((48, 48), Image.LANCZOS)
                    detail_photo = ImageTk.PhotoImage(img, master=win)
                except Exception:
                    detail_photo = None
            if not detail_photo and self.photos.get(uid):
                detail_photo = self.photos[uid]

        if detail_photo:
            win._detail_photo = detail_photo
            tk.Label(head, image=detail_photo, bg=BG).pack(side="left", padx=(0, 10))

        info_box = tk.Frame(head, bg=BG)
        info_box.pack(side="left", fill="x", expand=True)

        # 用户名
        name = match.get("name") or ""
        name_row = tk.Frame(info_box, bg=BG)
        name_row.pack(anchor="w", fill="x")
        tk.Label(name_row, text=name, bg=BG, fg=FG,
                 font=(theme.FONT, 13, "bold")).pack(side="left")
        if name:
            make_copy_btn(name_row, name).pack(side="left", padx=(8, 0))

        # 用户 ID
        if uid:
            uid_row = tk.Frame(info_box, bg=BG)
            uid_row.pack(anchor="w", fill="x", pady=(3, 0))
            tk.Label(uid_row, text=uid, bg=BG, fg=FG_DIM, font=TINY_FONT).pack(side="left")
            make_copy_btn(uid_row, uid).pack(side="left", padx=(8, 0))

        # 命中规则
        for rule in match.get("rules", []):
            line = tk.Frame(body, bg=BG)
            line.pack(fill="x", pady=(8, 0))
            tk.Label(line, text=rule.get("name", ""), bg=theme.CHIP_BG,
                     fg=rule.get("color") or theme.chip_color(rule.get("name", "")),
                     font=SMALL_FONT, padx=6, pady=1).pack(side="left")
            if rule.get("reason"):
                tk.Label(line, text="  " + rule["reason"], bg=BG, fg=FG_DIM,
                         font=SMALL_FONT).pack(side="left")

        # 共同好友
        mf_frame = tk.Frame(body, bg=BG)
        mf_frame.pack(fill="x", pady=(10, 0))
        mf_head = tk.Frame(mf_frame, bg=BG)
        mf_head.pack(fill="x")
        mf_title = tk.Label(mf_head, text="共同好友", bg=BG, fg=FG_DIM, font=SMALL_FONT)
        mf_title.pack(side="left")

        mf_content = tk.Frame(mf_frame, bg=BG)
        mf_content.pack(fill="x", pady=(4, 0))

        def render_mutuals(friends):
            for w in mf_content.winfo_children():
                w.destroy()
            if not friends:
                tk.Label(mf_content, text="（无公开共同好友）", bg=BG, fg=FG_DIM, font=TINY_FONT).pack(anchor="w")
                mf_title.config(text="共同好友 (0)")
                return
            mf_title.config(text="共同好友 (%d)" % len(friends))
            flow = tk.Frame(mf_content, bg=BG)
            flow.pack(fill="x")
            for i, f in enumerate(friends[:20]):
                f_name = f.get("displayName") or f.get("name") or "?"
                f_id = f.get("id") or f.get("userId") or ""
                item_chip = tk.Frame(flow, bg=BG_CARD, highlightthickness=1, highlightbackground=BORDER, padx=5, pady=2)
                item_chip.pack(side="left", padx=3, pady=2)
                tk.Label(item_chip, text=f_name, bg=BG_CARD, fg=FG, font=TINY_FONT).pack(side="left")
                if f_id:
                    make_copy_btn(item_chip, f_id, btn_text="复制ID").pack(side="left", padx=(4, 0))
            if len(friends) > 20:
                tk.Label(flow, text="等共 %d 人" % len(friends), bg=BG, fg=FG_DIM, font=TINY_FONT).pack(side="left", padx=4)

        me = self.matcher._get_me_uid() if hasattr(self.matcher, "_get_me_uid") else ""
        if not me or not uid:
            render_mutuals([])
        else:
            cached_friends = None
            if hasattr(self.matcher, "cache"):
                cf, fetched_at = self.matcher.cache.get_mutual_friends(me, uid)
                from matcher import MUTUAL_TTL
                if cf is not None and time.time() - fetched_at < MUTUAL_TTL:
                    cached_friends = cf

            if cached_friends is not None:
                render_mutuals(cached_friends)
            else:
                loading_lbl = tk.Label(mf_content, text="正在获取共同好友…", bg=BG, fg=FG_DIM, font=TINY_FONT)
                loading_lbl.pack(anchor="w")

                def fetch_task():
                    try:
                        flist = self.matcher._get_mutual_friends_list(me, uid)
                    except Exception:
                        flist = []

                    def do_render():
                        try:
                            if win.winfo_exists():
                                render_mutuals(flist or [])
                        except Exception:
                            pass

                    self.dispatch_ui(do_render)

                threading.Thread(target=fetch_task, daemon=True, name="FetchMutualDetail").start()

        # 简介
        bio_text = (match.get("profile") or {}).get("bio") or ""
        bio_head = tk.Frame(body, bg=BG)
        bio_head.pack(fill="x", pady=(12, 4))
        tk.Label(bio_head, text="简介", bg=BG, fg=FG_DIM, font=SMALL_FONT).pack(side="left")
        if bio_text:
            make_copy_btn(bio_head, bio_text, btn_text="复制简介").pack(side="left", padx=(8, 0))

        bio_box = tk.Text(body, height=8, wrap="word", relief="flat", bg="#131417", fg=FG,
                          insertbackground=FG, selectbackground="#34435c", selectforeground=FG,
                          font=SMALL_FONT, padx=8, pady=6, highlightthickness=1,
                          highlightbackground=BORDER)
        bio_box.insert("1.0", bio_text or "(无简介)")
        bio_box.config(state="disabled")
        bio_box.pack(fill="both", expand=True)

        # 外部链接
        links = (match.get("profile") or {}).get("bioLinks") or []
        if links:
            links_head = tk.Frame(body, bg=BG)
            links_head.pack(fill="x", pady=(10, 2))
            tk.Label(links_head, text="链接", bg=BG, fg=FG_DIM, font=SMALL_FONT).pack(side="left")

            for link in links:
                link_row = tk.Frame(body, bg=BG)
                link_row.pack(fill="x", pady=(2, 0))
                lbl = tk.Label(link_row, text=link, bg=BG, fg="#6cb2ff", cursor="hand2",
                               font=SMALL_FONT, anchor="w")
                lbl.pack(side="left")
                lbl.bind("<Button-1>", lambda e, u=link: webbrowser.open(u))
                make_copy_btn(link_row, link, btn_text="复制").pack(side="left", padx=(8, 0))
        return win
