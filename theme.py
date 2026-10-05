"""深色主题：颜色常量 + ttk 样式（基于 clam 主题，Windows 上可完全自定义配色）+ 高DPI适配。"""
import ctypes
import hashlib
import tkinter as tk
from tkinter import ttk

SCALE = 1.0   # 像素尺寸缩放系数（系统 DPI / 96），init_scaling() 后生效


def enable_high_dpi():
    """声明进程 DPI 感知，避免 Windows 把 96DPI 渲染的窗口位图拉伸导致模糊。

    必须在创建任何窗口（tk.Tk()）之前调用。
    """
    if not hasattr(ctypes, "windll"):
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def init_scaling(root):
    """创建 Tk root 后调用：计算像素缩放系数并应用到 ttk 像素尺寸。"""
    global SCALE
    try:
        SCALE = max(1.0, root.winfo_fpixels("1i") / 96.0)
    except Exception:
        SCALE = 1.0
    return SCALE


def px(n):
    """把按 96DPI 设计的像素尺寸换算成当前 DPI 的实际像素。"""
    return int(n * SCALE)


BG        = "#1e1f22"   # 主背景
BG_ALT    = "#26282b"   # 列表/次级背景
BG_CARD   = "#2b2d31"   # 卡片/按钮
BG_INPUT  = "#131417"   # 输入框
BG_HOVER  = "#33363b"
BORDER    = "#3a3d42"
FG        = "#e6e7ea"
FG_DIM    = "#9aa0a8"
ACCENT    = "#5aa2ff"
OK        = "#3ba55d"
WARN      = "#faa61a"
ERR       = "#ed4245"
LINK      = "#6cb2ff"

CHIP_COLORS = ["#7bd88f", "#69c0ff", "#f5b971", "#e397de", "#66d9e5", "#ffd76e", "#b3a3ff"]
CHIP_BG = "#33363b"

# 规则标签的可选颜色（18 色，色相彼此错开，深色底上都清晰）
RULE_COLORS = [
    "#ff5c5c",  # 红
    "#ff8c4d",  # 橙
    "#ffb84d",  # 琥珀
    "#ffe14d",  # 黄
    "#d6ff4d",  # 黄绿
    "#8cff5c",  # 绿
    "#4dff88",  # 翠
    "#4dffd2",  # 薄荷
    "#4de5ff",  # 天青
    "#4db8ff",  # 天蓝
    "#5c8cff",  # 蓝
    "#8c5cff",  # 靛
    "#b84dff",  # 紫
    "#e55cff",  # 品红
    "#ff5cd6",  # 玫
    "#ff5c8c",  # 粉红
    "#c0c0c0",  # 灰
    "#ffffff",  # 白
]

FONT = "Microsoft YaHei UI"


def chip_color(name):
    return CHIP_COLORS[int(hashlib.md5(name.encode("utf-8")).hexdigest(), 16) % len(CHIP_COLORS)]


def add_tooltip(widget, text):
    """悬停提示（不占布局空间）。"""
    if not text:
        return
    tip = {"win": None}

    def enter(_):
        if tip["win"]:
            return
        x = widget.winfo_rootx() + 10
        y = widget.winfo_rooty() + widget.winfo_height() + 2
        tw = tk.Toplevel(widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry("+%d+%d" % (x, y))
        tk.Label(tw, text=text, background="#0f1011", foreground="#e6e7ea",
                 relief="flat", font=(FONT, 8), justify="left", padx=6, pady=3).pack()
        tip["win"] = tw

    def leave(_):
        if tip["win"]:
            tip["win"].destroy()
            tip["win"] = None

    widget.bind("<Enter>", enter, add="+")
    widget.bind("<Leave>", leave, add="+")


def apply_dark_style(root):
    # Tk 原生控件全局后备样式（确保 Combobox 弹出列表、Listbox、Text、Menu 等在 Windows 下不回退到默认白色）
    for pattern, value in [
        ("*ComboboxPopdown.background", BG),
        ("*TCombobox*Listbox.background", BG_INPUT),
        ("*TCombobox*Listbox.foreground", FG),
        ("*TCombobox*Listbox.selectBackground", "#34435c"),
        ("*TCombobox*Listbox.selectForeground", FG),
        ("*TCombobox*Listbox.relief", "flat"),
        ("*TCombobox*Listbox.borderWidth", 0),
        ("*TCombobox*Listbox.highlightThickness", 1),
        ("*TCombobox*Listbox.highlightBackground", BORDER),
        ("*TCombobox*Listbox.highlightColor", BORDER),
        ("*TCombobox*Listbox.font", (FONT, 9)),
        ("*ComboboxListbox.background", BG_INPUT),
        ("*ComboboxListbox.foreground", FG),
        ("*ComboboxListbox.selectBackground", "#34435c"),
        ("*ComboboxListbox.selectForeground", FG),
        ("*ComboboxListbox.relief", "flat"),
        ("*ComboboxListbox.borderWidth", 0),
        ("*ComboboxListbox.highlightThickness", 1),
        ("*ComboboxListbox.highlightBackground", BORDER),
        ("*ComboboxListbox.highlightColor", BORDER),
        ("*ComboboxListbox.font", (FONT, 9)),
        ("*Listbox.background", BG_INPUT),
        ("*Listbox.foreground", FG),
        ("*Listbox.selectBackground", "#34435c"),
        ("*Listbox.selectForeground", FG),
        ("*Listbox.relief", "flat"),
        ("*Listbox.highlightBackground", BORDER),
        ("*Listbox.highlightColor", BORDER),
        ("*Text.background", BG_INPUT),
        ("*Text.foreground", FG),
        ("*Text.insertBackground", FG),
        ("*Text.selectBackground", "#34435c"),
        ("*Text.selectForeground", FG),
        ("*Entry.background", BG_INPUT),
        ("*Entry.foreground", FG),
        ("*Entry.insertBackground", FG),
        ("*Entry.selectBackground", "#34435c"),
        ("*Entry.selectForeground", FG),
        ("*Menu.background", "#0f1011"),
        ("*Menu.foreground", FG),
        ("*Menu.activeBackground", BG_HOVER),
        ("*Menu.activeForeground", FG),
        ("*Toplevel.background", BG),
    ]:
        root.option_add(pattern, value)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(".", background=BG, foreground=FG, bordercolor=BORDER,
                    lightcolor=BG, darkcolor=BG, troughcolor=BG_ALT,
                    focuscolor=BG, selectbackground="#3b5a7a", selectforeground=FG,
                    font=(FONT, 9))
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=FG)
    style.configure("Dim.TLabel", background=BG, foreground=FG_DIM)
    style.configure("Head.TLabel", background=BG, foreground=FG, font=(FONT, 10, "bold"))

    style.configure("TButton", background=BG_CARD, foreground=FG, borderwidth=0,
                    padding=(px(10), px(4)), relief="flat")
    style.map("TButton", background=[("active", BG_HOVER), ("disabled", BG_ALT)],
              foreground=[("disabled", FG_DIM)])
    style.configure("Tool.TButton", background=BG_CARD, foreground=FG, borderwidth=0,
                    padding=(px(7), px(3)), relief="flat", font=(FONT, 9))
    style.map("Tool.TButton", background=[("active", BG_HOVER), ("disabled", BG_ALT)],
              foreground=[("disabled", FG_DIM)])
    style.configure("Accent.TButton", background="#2d5a9e", foreground="#ffffff")
    style.map("Accent.TButton", background=[("active", "#3a6cbb"), ("disabled", BG_CARD)])

    style.configure("TEntry", fieldbackground=BG_INPUT, foreground=FG,
                    insertcolor=FG, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    padding=px(3))
    style.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)])
    style.configure("TText", background=BG_INPUT, foreground=FG)

    # 下拉选择框（深色背景、暗色输入底色、只读状态与弹出列表完全适配）
    style.configure("TCombobox",
                    fieldbackground=BG_INPUT,
                    background=BG_CARD,
                    foreground=FG,
                    arrowcolor=FG,
                    bordercolor=BORDER,
                    lightcolor=BORDER,
                    darkcolor=BORDER,
                    insertcolor=FG,
                    relief="flat",
                    padding=px(3))
    style.map("TCombobox",
              fieldbackground=[("readonly", "focus", BG_INPUT),
                               ("readonly", BG_INPUT),
                               ("disabled", BG_ALT)],
              background=[("active", BG_HOVER), ("disabled", BG_ALT)],
              foreground=[("disabled", FG_DIM)],
              arrowcolor=[("disabled", FG_DIM)],
              bordercolor=[("focus", ACCENT)],
              lightcolor=[("focus", ACCENT)],
              selectbackground=[("readonly", BG_INPUT)],
              selectforeground=[("readonly", FG)])

    # 微调数值框
    style.configure("TSpinbox",
                    fieldbackground=BG_INPUT,
                    background=BG_CARD,
                    foreground=FG,
                    arrowcolor=FG,
                    bordercolor=BORDER,
                    lightcolor=BORDER,
                    darkcolor=BORDER,
                    insertcolor=FG,
                    relief="flat",
                    padding=px(3))
    style.map("TSpinbox",
              fieldbackground=[("readonly", BG_INPUT), ("disabled", BG_ALT)],
              background=[("active", BG_HOVER), ("disabled", BG_ALT)],
              foreground=[("disabled", FG_DIM)],
              arrowcolor=[("disabled", FG_DIM)],
              bordercolor=[("focus", ACCENT)],
              lightcolor=[("focus", ACCENT)])

    style.configure("TCheckbutton", background=BG, foreground=FG, focuscolor=BG)
    style.map("TCheckbutton", background=[("active", BG)])
    style.configure("TRadiobutton", background=BG, foreground=FG, focuscolor=BG)
    style.map("TRadiobutton", background=[("active", BG)])

    style.configure("Treeview", background=BG_ALT, fieldbackground=BG_ALT,
                    foreground=FG, borderwidth=0, rowheight=px(26))
    style.configure("Treeview.Heading", background=BG_CARD, foreground=FG_DIM,
                    borderwidth=0, padding=(px(4), px(3)))
    style.map("Treeview", background=[("selected", "#34435c")],
              foreground=[("selected", FG)],
              fieldbackground=[("disabled", BG_ALT)])

    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(px(6), px(6), px(6), 0))
    # 未选中标签：与页面同色的扁平暗灰；选中标签：更亮、更大、无描边 —— 明确区分
    style.configure("TNotebook.Tab", background=BG, foreground=FG_DIM,
                    padding=(px(12), px(5)), borderwidth=0,
                    lightcolor=BG, darkcolor=BG, bordercolor=BG)
    style.map("TNotebook.Tab",
              background=[("selected", BG_HOVER)],
              foreground=[("selected", FG)],
              lightcolor=[("selected", BG_HOVER)],
              darkcolor=[("selected", BG_HOVER)],
              bordercolor=[("selected", BG_HOVER)],
              padding=[("selected", (px(16), px(7)))],
              expand=[("selected", (px(1), px(1), px(1), 0))])

    style.configure("TLabelframe", background=BG, borderwidth=0)
    style.configure("TLabelframe.Label", background=BG, foreground=FG_DIM)
    style.configure("TSeparator", background=BORDER)
    style.configure("Horizontal.TScale", background=BG, troughcolor=BG_ALT,
                    bordercolor=BORDER, lightcolor=ACCENT, darkcolor=ACCENT)
    style.configure("TScrollbar", background=BG_CARD, troughcolor=BG,
                    bordercolor=BG, arrowcolor=FG_DIM, relief="flat")
