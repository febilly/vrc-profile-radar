"""主面板：可视化规则编辑器 + 设置（Cookie / LLM / 扫描参数 / 悬浮窗外观）。

所有更改即时生效并自动保存（内存立即更新、写盘防抖合并、副作用去抖应用），无保存按钮。
与悬浮窗分离：悬浮窗只负责游戏内显示匹配结果；主面板负责配置，关掉即隐藏。
"""
import json
import os
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog

import theme
from theme import BG, BG_INPUT, BORDER, FG, FG_DIM, OK, ERR, WARN
from rules import (ALL_FIELDS, KEYWORD, REGEX, LLM, EXCLUDE, MUTUAL, Rule,
                   OPERATOR_DISPLAY, load_rules_payload, rule_signature, rules_payload)
from overlay import short_text

TYPE_LABEL = {KEYWORD: "关键词", REGEX: "正则", LLM: "AI判断",
              EXCLUDE: "排除", MUTUAL: "共同好友"}
FIELD_LABELS = {"bio": "简介", "bioLinks": "链接", "displayName": "昵称",
                "pronouns": "代词", "statusDescription": "状态描述", "tags": "标签"}

# 共同好友规则：界面显示符号 <-> 配置存储的运算符
OP_DISPLAY_LIST = ["≥", ">", "=", "≤", "<"]
OP_FROM_DISPLAY = {"≥": ">=", ">": ">", "=": "=", "≤": "<=", "<": "<"}
OP_TO_DISPLAY = {v: k for k, v in OP_FROM_DISPLAY.items()}

INT_KEYS = {("llm", "batch_size"), ("llm", "concurrency"), ("llm", "result_cache_max")}
FLOAT_KEYS = {("api", "requests_per_second"), ("scan", "debounce_seconds"), ("api", "profile_cache_hours")}
APPLY_DEBOUNCE_MS = 500
WRITEBACK_DEBOUNCE_MS = 300


def btn(parent, text, command, style="Tool.TButton"):
    """带显式宽度的 ttk 按钮：本机 ttk 按钮不给 width 时会取一个与文本无关的固定宽度。"""
    units = sum(2 if ord(c) > 127 else 1 for c in text)
    return ttk.Button(parent, text=text, command=command, style=style, width=units + 2)


def rule_summary(rule):
    t = rule.get("type")
    if t == KEYWORD:
        return short_text("、".join(rule.get("keywords") or []), 14)
    if t == REGEX:
        return short_text(rule.get("pattern") or "", 14)
    if t == LLM:
        return short_text(rule.get("prompt") or "", 14)
    if t == EXCLUDE:
        parts = []
        if rule.get("exclude_self"):
            parts.append("自己")
        if rule.get("exclude_friends"):
            parts.append("好友")
        users = rule.get("users") or []
        if users:
            parts.append("名单%d人" % len(users))
        return "排除：" + ("、".join(parts) if parts else "未选条件")
    if t == MUTUAL:
        op_str = OPERATOR_DISPLAY.get(rule.get("operator", ">="), "≥")
        cnt = rule.get("count", 5)
        if rule.get("limit_users"):
            u_len = len(rule.get("users") or [])
            return "指定共同好友%s%s人（名单%d人）" % (op_str, cnt, u_len)
        return "共同好友%s%s人" % (op_str, cnt)
    for k in ("server_domain", "admin_url_template", "pattern", "description"):
        v = rule.get(k)
        if v:
            return short_text(str(v), 16)
    return "插件规则"


def tree_values(rule, matcher=None):
    rtype = rule.get("type")
    type_name = TYPE_LABEL.get(rtype)
    if not type_name and matcher and getattr(matcher, "plugin_manager", None):
        rt_def = matcher.plugin_manager.get_rule_type(rtype)
        if rt_def:
            type_name = rt_def.get("name")
    if not type_name:
        type_name = rtype
    return ("✓" if rule.get("enabled", True) else "×",
            short_text(rule.get("name", ""), 10),
            type_name,
            rule_summary(rule))


class MainWindow:
    def __init__(self, root, config, matcher, apply_hooks, overlay=None, on_quit=None):
        self.config = config
        self.matcher = matcher
        self.apply_hooks = list(apply_hooks)
        self.on_quit = on_quit
        self.overlay = overlay

        self.top = tk.Toplevel(root)
        self.top.title("VRChat Profile 雷达" + ("（演示模式）" if getattr(root, "_demo", False) else ""))
        w = int(config.get("ui", "main_w", default=760) * theme.SCALE)
        h = int(config.get("ui", "main_h", default=600) * theme.SCALE)
        w = min(w, int(self.top.winfo_screenwidth() * 0.9))
        h = min(h, int(self.top.winfo_screenheight() * 0.9))
        self.top.geometry("%dx%d" % (w, h))
        self.top.configure(bg=BG)
        # 关闭主窗口 = 退出整个程序（悬浮窗一并关闭）
        self.top.protocol("WM_DELETE_WINDOW", self._on_close)

        notebook = ttk.Notebook(self.top)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)
        self.rule_editor = RuleEditor(notebook, config, get_api=lambda: self.matcher.api,
                                      on_rules_changed=self._on_rules_changed, matcher=matcher)
        self.status_tab = StatusTab(notebook, matcher)
        self.plugins_tab = PluginsTab(notebook, matcher, config,
                                      on_plugins_changed=self._on_plugins_changed)
        self.settings_tab = SettingsTab(notebook, config, matcher, self.apply_hooks,
                                        overlay=overlay, on_rules_changed=self._on_rules_imported)
        notebook.add(self.rule_editor, text=" 规则 ")
        notebook.add(self.status_tab, text=" 状态 ")
        notebook.add(self.plugins_tab, text=" 插件 ")
        notebook.add(self.settings_tab, text=" 设置 ")
        notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _on_tab_changed(self, event=None):
        if hasattr(self, "rule_editor") and getattr(self.rule_editor, "_pending_wb", None):
            self.rule_editor._flush_wb()

    def _on_plugins_changed(self):
        if hasattr(self, "rule_editor"):
            try:
                self.rule_editor._refresh_plugin_buttons()
                self.rule_editor._reload_tree()
            except Exception:
                pass
        self._on_rules_changed()

    def _on_rules_changed(self):
        if self.matcher:
            self.matcher.rescan(immediate=True)
        if hasattr(self, "status_tab"):
            try:
                self.status_tab._refresh()
            except Exception:
                pass

    def _on_rules_imported(self):
        self.rule_editor._reload_tree()
        self._on_rules_changed()

    def _reload_rules(self):
        self._on_rules_imported()

    def show(self):
        self.top.deiconify()
        self.top.lift()
        self.top.focus_force()
        self.settings_tab.refresh()

    def _on_close(self):
        if hasattr(self, "rule_editor") and getattr(self.rule_editor, "_pending_wb", None):
            self.rule_editor._flush_wb()
        if self.on_quit:
            self.on_quit()
        else:
            self.top.destroy()


class RuleEditor(ttk.Frame):
    def __init__(self, parent, config, get_api=None, on_rules_changed=None, matcher=None):
        super().__init__(parent, padding=6)
        self.config = config
        self.get_api = get_api or (lambda: None)
        self.on_rules_changed = on_rules_changed
        self.matcher = matcher
        self.selected_id = None
        self._pending_wb = {}     # rule_id -> writeback 函数
        self._wb_after = None

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        btn(bar, "＋关键词", lambda: self._add(KEYWORD)).pack(side="left")
        btn(bar, "＋正则", lambda: self._add(REGEX)).pack(side="left", padx=(4, 0))
        btn(bar, "＋AI", lambda: self._add(LLM)).pack(side="left", padx=(4, 0))
        btn(bar, "＋排除", lambda: self._add(EXCLUDE)).pack(side="left", padx=(4, 0))
        btn(bar, "＋共同好友", lambda: self._add(MUTUAL)).pack(side="left", padx=(4, 0))

        self.plugin_btn_frame = ttk.Frame(bar)
        self.plugin_btn_frame.pack(side="left", padx=(4, 0))
        self._refresh_plugin_buttons()

        btn(bar, "复制", self._copy_rule).pack(side="left", padx=(12, 0))
        btn(bar, "删除", self._delete).pack(side="left", padx=(4, 0))
        btn(bar, "上移", lambda: self._move(-1)).pack(side="left", padx=(12, 0))
        btn(bar, "下移", lambda: self._move(1)).pack(side="left", padx=(4, 0))

        columns = ("enabled", "name", "type", "summary")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=7, selectmode="browse")
        for col, text, width in (("enabled", "启用", theme.px(46)), ("name", "名称", theme.px(200)),
                                 ("type", "类型", theme.px(70)), ("summary", "内容", theme.px(310))):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="x")
        self.tree.tag_configure("dim", foreground=FG_DIM)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        self.form_holder = ttk.Frame(self)
        self.form_holder.pack(fill="both", expand=True, pady=(8, 0))

        self._reload_tree()
        if self.tree.get_children():
            self.tree.selection_set(self.tree.get_children()[0])

    # ---- 即时写回 ----

    def _queue_wb(self, rule_id, fn):
        self._pending_wb[rule_id] = fn
        if self._wb_after:
            self.after_cancel(self._wb_after)
        self._wb_after = self.after(WRITEBACK_DEBOUNCE_MS, self._flush_wb)

    def _flush_wb(self):
        if self._wb_after:
            self.after_cancel(self._wb_after)
            self._wb_after = None
        fns = list(self._pending_wb.values())
        self._pending_wb.clear()
        changed = False
        for fn in fns:
            try:
                fn()
                changed = True
            except Exception:
                pass
        if changed and self.on_rules_changed:
            try:
                self.on_rules_changed()
            except Exception:
                pass

    def _refresh_tree_item(self, rule):
        iid = rule.get("id")
        if iid and self.tree.exists(iid):
            tags = self._rule_tags(rule)
            if tags and tags[0].startswith("c:"):
                self.tree.tag_configure(tags[0], foreground=tags[0][2:])
            self.tree.item(iid, values=tree_values(rule, self.matcher), tags=tags)

    # ---- 列表 ----

    def _rules(self):
        return self.config.rules

    @staticmethod
    def _rule_tags(rule):
        """行标签：自定义颜色的规则整行文字用该颜色，禁用的置灰。"""
        if not rule.get("enabled", True):
            return ("dim",)
        color = str(rule.get("color") or "").strip()
        return ("c:" + color,) if color else ()

    def _reload_tree(self):
        selected = self.selected_id
        self.tree.delete(*self.tree.get_children())
        for rule in self._rules():
            tags = self._rule_tags(rule)
            if tags and tags[0].startswith("c:"):
                self.tree.tag_configure(tags[0], foreground=tags[0][2:])
            self.tree.insert("", "end", iid=rule.get("id", ""),
                             values=tree_values(rule, self.matcher), tags=tags)
        if selected and selected in self.tree.get_children():
            self.tree.selection_set(selected)
        elif self.tree.get_children():
            self.tree.selection_set(self.tree.get_children()[0])

    def _refresh_plugin_buttons(self):
        if not hasattr(self, "plugin_btn_frame"):
            return
        for w in self.plugin_btn_frame.winfo_children():
            w.destroy()
        pm = getattr(self.matcher, "plugin_manager", None) if self.matcher else None
        if not pm:
            return
        for rt in pm.get_available_rule_types():
            name = rt.get("name") or "插件规则"
            btn(self.plugin_btn_frame, "＋" + name, lambda t=rt: self._add_plugin_rule(t)).pack(side="left", padx=(2, 0))

    def _add_plugin_rule(self, rt):
        rules = self._rules()
        new_rule = {
            "id": self.config.new_rule_id(),
            "type": rt.get("type", "plugin"),
            "plugin_id": rt.get("plugin_id", ""),
            "name": rt.get("default_name") or rt.get("name") or "新插件规则",
            "color": rt.get("default_color") or "#ffd700",
            "enabled": True,
        }
        for f in rt.get("fields", []):
            k = f.get("key")
            if k:
                new_rule[k] = f.get("default", "")
        rules.append(new_rule)
        self._save_rules(rules, select_id=new_rule["id"])

    def _on_select(self, _=None):
        if self._pending_wb:
            self._flush_wb()
        selection = self.tree.selection()
        if not selection:
            return
        self.selected_id = selection[0]
        rule = next((r for r in self._rules() if r.get("id") == self.selected_id), None)
        if rule:
            self._build_form(rule)

    # ---- 增删改 ----

    def _add(self, rule_type):
        rules = self._rules()
        defaults = {
            KEYWORD: {"name": "新关键词规则", "keywords": [], "mode": "any", "fields": ["bio"]},
            REGEX: {"name": "新正则规则", "pattern": "", "fields": ["bio", "bioLinks"]},
            LLM: {"name": "新AI规则", "prompt": ""},
            EXCLUDE: {"name": "排除规则", "exclude_self": True, "exclude_friends": False,
                      "users": []},
            MUTUAL: {"name": "新共同好友规则", "operator": ">=", "count": 5,
                     "limit_users": False, "users": []},
        }
        rule = {"id": self.config.new_rule_id(), "type": rule_type, "enabled": True,
                **defaults[rule_type]}
        rules.append(rule)
        self._save_rules(rules, select_id=rule["id"])

    def _copy_rule(self):
        rule = self._current_rule()
        if not rule:
            return
        import copy
        new_rule = copy.deepcopy(rule)
        new_rule["id"] = self.config.new_rule_id()
        new_rule["name"] = rule.get("name", "") + " 副本"
        rules = self._rules()
        rules.insert(rules.index(rule) + 1, new_rule)
        self._save_rules(rules, select_id=new_rule["id"])

    def _delete(self):
        rule = self._current_rule()
        if not rule:
            return
        rules = [r for r in self._rules() if r is not rule]
        self._pending_wb.pop(rule.get("id"), None)
        self.selected_id = None
        self._save_rules(rules)

    def _move(self, delta):
        rule = self._current_rule()
        if not rule:
            return
        rules = self._rules()
        i = rules.index(rule)
        j = i + delta
        if 0 <= j < len(rules):
            rules[i], rules[j] = rules[j], rules[i]
            self._save_rules(rules, select_id=rule["id"])

    def _current_rule(self):
        return next((r for r in self._rules() if r.get("id") == self.selected_id), None)

    def _save_rules(self, rules, select_id=None):
        self.config.set(rules, "rules")
        self.selected_id = select_id or self.selected_id
        self._reload_tree()
        if self.on_rules_changed:
            try:
                self.on_rules_changed()
            except Exception:
                pass

    # ---- 表单（更改即时写回配置） ----

    def _build_form(self, rule):
        for child in self.form_holder.winfo_children():
            child.destroy()
        form = ttk.Frame(self.form_holder)
        form.pack(fill="both", expand=True)
        rule_id = rule.get("id")

        err_label = ttk.Label(form, text="")
        row = 0

        ttk.Label(form, text="名称").grid(row=row, column=0, sticky="e", padx=(0, 6), pady=4)
        name_var = tk.StringVar(value=rule.get("name", ""))
        name_entry = ttk.Entry(form, textvariable=name_var, width=18)
        name_entry.grid(row=row, column=1, sticky="w", pady=4)

        # 标签颜色：色板放在名称与启用之间的空闲空间，选中色用于悬浮窗标签文字
        color_selected = {"v": str(rule.get("color") or "")}
        color_frame = ttk.Frame(form)
        color_frame.grid(row=row, column=2, sticky="w", padx=(12, 0))
        swatches = []
        for c in theme.RULE_COLORS:
            sw = tk.Frame(color_frame, width=theme.px(14), height=theme.px(14),
                          background=c, highlightthickness=1,
                          highlightbackground=BORDER, highlightcolor=BORDER,
                          cursor="hand2")
            sw.pack(side="left", padx=theme.px(1))
            sw.pack_propagate(False)
            swatches.append((sw, c))
            sw.bind("<Button-1>", lambda e, cc=c: pick(cc))
        theme.add_tooltip(color_frame, "标签颜色：点选应用；再点一次已选颜色恢复自动取色")

        def repaint_color():
            for sw, c in swatches:
                sel = color_selected["v"] == c
                sw.configure(highlightthickness=theme.px(2) if sel else 1,
                             highlightbackground=FG if sel else BORDER,
                             highlightcolor=FG if sel else BORDER)

        def pick(cc):
            color_selected["v"] = "" if color_selected["v"] == cc else cc
            repaint_color()
            flush_now()

        repaint_color()

        enabled_var = tk.BooleanVar(value=rule.get("enabled", True))
        ttk.Checkbutton(form, text="启用", variable=enabled_var, command=lambda: flush_now()).grid(
            row=row, column=3, sticky="w", padx=(12, 0), pady=4)
        row += 1

        field_vars = {}
        keywords = []
        mode_var = None
        pattern_var = None
        prompt_text = None
        test_result = None
        kw_listbox = None

        if rule.get("type") == KEYWORD:
            keywords = list(rule.get("keywords") or [])
            ttk.Label(form, text="关键词（逐条管理）").grid(row=row, column=0, sticky="ne", padx=(0, 6), pady=4)
            kw_box = ttk.Frame(form)
            kw_box.grid(row=row, column=1, columnspan=2, sticky="nsew", pady=4)

            list_wrap = ttk.Frame(kw_box)
            list_wrap.pack(side="left", fill="both", expand=True)
            kw_listbox = tk.Listbox(list_wrap, height=6, bg=BG_INPUT, fg=FG, relief="flat",
                                    highlightthickness=1, highlightbackground=BORDER,
                                    selectbackground="#34435c", font=(theme.FONT, 9),
                                    exportselection=False, activestyle="none")
            kw_listbox.pack(side="left", fill="both", expand=True)
            kw_scroll = ttk.Scrollbar(list_wrap, orient="vertical", command=kw_listbox.yview)
            kw_scroll.pack(side="left", fill="y")
            kw_listbox.config(yscrollcommand=kw_scroll.set)
            for k in keywords:
                kw_listbox.insert("end", k)

            kw_tools = ttk.Frame(kw_box)
            kw_tools.pack(side="left", fill="y", padx=(8, 0))
            new_kw_var = tk.StringVar()
            new_kw_entry = ttk.Entry(kw_tools, textvariable=new_kw_var, width=16)
            new_kw_entry.pack(fill="x")

            def add_kw(_=None):
                k = new_kw_var.get().strip()
                if not k:
                    return
                if k in keywords:
                    err_label.config(text="关键词已存在：%s" % short_text(k, 12), foreground=WARN)
                    return
                keywords.append(k)
                kw_listbox.insert("end", k)
                new_kw_var.set("")
                err_label.config(text="")
                flush_now()

            def del_kw(_=None):
                for i in reversed(kw_listbox.curselection()):
                    del keywords[i]
                    kw_listbox.delete(i)
                flush_now()

            btn(kw_tools, "添加", add_kw).pack(fill="x", pady=(6, 0))
            btn(kw_tools, "删除选中", del_kw).pack(fill="x", pady=(4, 0))
            new_kw_entry.bind("<Return>", add_kw)
            kw_listbox.bind("<Double-Button-1>", del_kw)
            theme.add_tooltip(new_kw_entry, "输入关键词后按回车或点\"添加\"")
            theme.add_tooltip(kw_listbox, "双击条目可删除")
            row += 1
            ttk.Label(form, text="匹配方式").grid(row=row, column=0, sticky="e", padx=(0, 6), pady=4)
            mode_var = tk.StringVar(value=rule.get("mode", "any"))
            mode_box = ttk.Frame(form)
            mode_box.grid(row=row, column=1, columnspan=2, sticky="w", pady=4)
            ttk.Radiobutton(mode_box, text="命中任一", variable=mode_var, value="any",
                            command=lambda: flush_now()).pack(side="left")
            ttk.Radiobutton(mode_box, text="全部命中", variable=mode_var, value="all",
                            command=lambda: flush_now()).pack(side="left", padx=(10, 0))
            row += 1
        elif rule.get("type") == REGEX:
            ttk.Label(form, text="正则表达式").grid(row=row, column=0, sticky="e", padx=(0, 6), pady=4)
            pattern_var = tk.StringVar(value=rule.get("pattern", ""))
            ttk.Entry(form, textvariable=pattern_var, width=42).grid(row=row, column=1, columnspan=2, sticky="we", pady=4)
            row += 1
            ttk.Label(form, text="测试文本").grid(row=row, column=0, sticky="e", padx=(0, 6), pady=4)
            test_var = tk.StringVar()
            ttk.Entry(form, textvariable=test_var, width=42).grid(row=row, column=1, columnspan=2, sticky="we", pady=4)
            row += 1
            test_result = ttk.Label(form, text="", foreground=FG_DIM)
            test_result.grid(row=row, column=1, columnspan=2, sticky="w", pady=(0, 4))
            row += 1

            def update_test(_=None):
                pattern = pattern_var.get()
                try:
                    compiled = re.compile(pattern, re.IGNORECASE)
                except re.error as exc:
                    test_result.config(text="正则错误：%s（错误时该规则不会命中任何玩家）" % exc, foreground=WARN)
                    return
                m = compiled.search(test_var.get())
                if not test_var.get():
                    test_result.config(text="在测试文本里输入一段简介试试", foreground=FG_DIM)
                elif m:
                    test_result.config(text="命中：%s" % m.group(0), foreground=OK)
                else:
                    test_result.config(text="未命中", foreground=FG_DIM)

            pattern_var.trace_add("write", lambda *_: (update_test(), queue()))
            test_var.trace_add("write", update_test)
        elif rule.get("type") == LLM:
            ttk.Label(form, text="判断要求（发给大模型）").grid(row=row, column=0, sticky="ne", padx=(0, 6), pady=4)
            prompt_text = tk.Text(form, height=5, wrap="word", bg=BG_INPUT, fg=FG,
                                  insertbackground=FG, selectbackground="#34435c", selectforeground=FG,
                                  relief="flat", padx=6, pady=4,
                                  highlightthickness=1, highlightbackground=BORDER,
                                  font=(theme.FONT, 9))
            prompt_text.insert("1.0", rule.get("prompt", ""))
            prompt_text.grid(row=row, column=1, columnspan=2, sticky="nsew", pady=4)

            def on_prompt_modified(_=None):
                prompt_text.edit_modified(False)
                queue()

            prompt_text.bind("<<Modified>>", on_prompt_modified)
            row += 1
        elif rule.get("type") == EXCLUDE:
            self_var = tk.BooleanVar(value=rule.get("exclude_self", False))
            ttk.Checkbutton(form, text="排除自己（游戏客户端登录的玩家）", variable=self_var,
                            command=lambda: flush_now()).grid(row=row, column=1, sticky="w", pady=4)
            row += 1
            friends_var = tk.BooleanVar(value=rule.get("exclude_friends", False))
            ttk.Checkbutton(form, text="排除好友（Cookie 账号的好友）", variable=friends_var,
                            command=lambda: flush_now()).grid(row=row, column=1, sticky="w", pady=4)
            row += 1
            ttk.Label(form, text="排除名单（按 uid）").grid(row=row, column=0, sticky="ne", padx=(0, 6), pady=4)
            list_row = row
            users = [dict(u) for u in (rule.get("users") or [])]
            kw_box = ttk.Frame(form)
            kw_box.grid(row=row, column=1, columnspan=2, sticky="nsew", pady=4)

            list_wrap = ttk.Frame(kw_box)
            list_wrap.pack(side="left", fill="both", expand=True)
            users_listbox = tk.Listbox(list_wrap, height=6, bg=BG_INPUT, fg=FG, relief="flat",
                                       highlightthickness=1, highlightbackground=BORDER,
                                       selectbackground="#34435c", font=(theme.FONT, 9),
                                       exportselection=False, activestyle="none")
            users_listbox.pack(side="left", fill="both", expand=True)
            users_scroll = ttk.Scrollbar(list_wrap, orient="vertical", command=users_listbox.yview)
            users_scroll.pack(side="left", fill="y")
            users_listbox.config(yscrollcommand=users_scroll.set)

            def render_users():
                users_listbox.delete(0, "end")
                for u in users:
                    users_listbox.insert("end", "%s  (%s)" % (u.get("name", "?"), u.get("user_id", "")))

            for u in users:
                users_listbox.insert("end", "%s  (%s)" % (u.get("name", "?"), u.get("user_id", "")))

            def del_user(_=None):
                for i in reversed(users_listbox.curselection()):
                    del users[i]
                    users_listbox.delete(i)
                flush_now()

            def search_add():
                from user_search_dialog import UserSearchDialog

                def on_pick(user):
                    uid = user.get("id", "")
                    if not uid:
                        return
                    if any(u.get("user_id") == uid for u in users):
                        err_label.config(text="已在名单里：%s" % short_text(
                            user.get("displayName", ""), 12), foreground=WARN)
                        return
                    users.append({"user_id": uid, "name": user.get("displayName", "")})
                    render_users()
                    err_label.config(text="")
                    flush_now()

                UserSearchDialog(self, self.get_api, on_pick, title="搜索玩家加入排除名单")

            tools = ttk.Frame(kw_box)
            tools.pack(side="left", fill="y", padx=(8, 0))
            btn(tools, "搜索添加…", search_add).pack(fill="x", pady=(6, 0))
            btn(tools, "删除选中", del_user).pack(fill="x", pady=(4, 0))
            users_listbox.bind("<Double-Button-1>", del_user)
            theme.add_tooltip(users_listbox, "双击条目可从名单移除")
            row += 1
        elif rule.get("type") == MUTUAL:
            ttk.Label(form, text="共同好友数量").grid(row=row, column=0, sticky="e", padx=(0, 6), pady=4)
            op_box = ttk.Frame(form)
            op_box.grid(row=row, column=1, columnspan=2, sticky="w", pady=4)
            op_var = tk.StringVar(value=OP_TO_DISPLAY.get(rule.get("operator", ">="), "≥"))
            ttk.Combobox(op_box, textvariable=op_var, values=OP_DISPLAY_LIST,
                         width=4, state="readonly").pack(side="left")
            count_var = tk.StringVar(value=str(rule.get("count", 5)))
            ttk.Entry(op_box, textvariable=count_var, width=8).pack(side="left", padx=(8, 0))
            ttk.Label(op_box, text="人", style="Dim.TLabel").pack(side="left", padx=(4, 0))
            hint_lbl = ttk.Label(op_box, text="", style="Dim.TLabel")
            hint_lbl.pack(side="left", padx=(4, 0))
            op_var.trace_add("write", lambda *_: flush_now())
            count_var.trace_add("write", lambda *_: queue())
            row += 1

            ttk.Label(form, text="💡 提示：规则名称支持 {count} 占位符（如「共同好友({count})」），悬浮窗将自动显示实际好友数",
                      style="Dim.TLabel").grid(row=row, column=1, columnspan=2, sticky="w", pady=(0, 4))
            row += 1

            limit_users_var = tk.BooleanVar(value=bool(rule.get("limit_users", False)))
            mutual_users = [dict(u) for u in (rule.get("users") or [])]

            users_frame = ttk.Frame(form)
            list_row = row + 1

            def update_limit_ui():
                if limit_users_var.get():
                    hint_lbl.config(text="（仅统计指定名单中属于对方公开共同好友的人数）")
                    users_frame.grid(row=list_row, column=0, columnspan=3, sticky="nsew", pady=(2, 4))
                else:
                    hint_lbl.config(text="（与 Cookie 账号的共同好友，不含共同群组；对方隐藏的共同好友也计入）")
                    users_frame.grid_remove()

            ttk.Checkbutton(form, text="仅从指定名单中筛选共同好友（只看与对方的共同好友是否属于所选名单）",
                            variable=limit_users_var,
                            command=lambda: (update_limit_ui(), flush_now())).grid(
                                row=row, column=1, columnspan=2, sticky="w", pady=4)
            row += 1

            # 构建指定共同好友名单管理面板
            ttk.Label(users_frame, text="指定共同好友名单\n（按 uid）").grid(
                row=0, column=0, sticky="ne", padx=(0, 6), pady=4)
            kw_box = ttk.Frame(users_frame)
            kw_box.grid(row=0, column=1, columnspan=2, sticky="nsew", pady=4)

            list_wrap = ttk.Frame(kw_box)
            list_wrap.pack(side="left", fill="both", expand=True)
            m_listbox = tk.Listbox(list_wrap, height=6, bg=BG_INPUT, fg=FG, relief="flat",
                                   highlightthickness=1, highlightbackground=BORDER,
                                   selectbackground="#34435c", selectforeground=FG,
                                   font=(theme.FONT, 9), exportselection=False, activestyle="none")
            m_listbox.pack(side="left", fill="both", expand=True)
            m_scroll = ttk.Scrollbar(list_wrap, orient="vertical", command=m_listbox.yview)
            m_scroll.pack(side="left", fill="y")
            m_listbox.config(yscrollcommand=m_scroll.set)

            def render_m_users():
                m_listbox.delete(0, "end")
                for u in mutual_users:
                    m_listbox.insert("end", "%s  (%s)" % (u.get("name", "?"), u.get("user_id", "")))

            render_m_users()

            def del_m_user(_=None):
                for i in reversed(m_listbox.curselection()):
                    del mutual_users[i]
                    m_listbox.delete(i)
                flush_now()

            def search_add_m():
                from user_search_dialog import UserSearchDialog

                def on_pick(user):
                    uid = user.get("id", "")
                    if not uid:
                        return
                    if any(u.get("user_id") == uid for u in mutual_users):
                        err_label.config(text="已在名单里：%s" % short_text(
                            user.get("displayName", ""), 12), foreground=WARN)
                        return
                    mutual_users.append({"user_id": uid, "name": user.get("displayName", "")})
                    render_m_users()
                    err_label.config(text="")
                    flush_now()

                UserSearchDialog(self, self.get_api, on_pick, title="搜索玩家加入指定共同好友名单")

            tools = ttk.Frame(kw_box)
            tools.pack(side="left", fill="y", padx=(8, 0))
            btn(tools, "搜索添加…", search_add_m).pack(fill="x", pady=(4, 0))
            btn(tools, "删除选中", del_m_user).pack(fill="x", pady=(4, 0))
            m_listbox.bind("<Double-Button-1>", del_m_user)
            theme.add_tooltip(m_listbox, "双击条目可从名单移除")

            ttk.Label(users_frame, text="（注：仅可比对对方公开可见的共同好友；若对方在隐私设置中隐藏了共同好友列表则无法比对）",
                      style="Dim.TLabel").grid(row=1, column=1, columnspan=2, sticky="w", pady=(2, 0))
            users_frame.columnconfigure(1, weight=1)

            update_limit_ui()
            row += 1

        if rule.get("type") in (KEYWORD, REGEX):
            ttk.Label(form, text="匹配范围").grid(row=row, column=0, sticky="e", padx=(0, 6), pady=4)
            fields_box = ttk.Frame(form)
            fields_box.grid(row=row, column=1, columnspan=2, sticky="w", pady=4)
            for field_name in ALL_FIELDS:
                var = tk.BooleanVar(value=field_name in (rule.get("fields") or ["bio"]))
                field_vars[field_name] = var
                ttk.Checkbutton(fields_box, text=FIELD_LABELS[field_name], variable=var,
                                command=lambda: flush_now()).pack(side="left")
            row += 1

        plugin_field_vars = {}
        pm = getattr(self.matcher, "plugin_manager", None) if self.matcher else None
        rt_def = pm.get_rule_type(rule.get("type")) if pm else None
        if rule.get("type") not in (KEYWORD, REGEX, LLM, EXCLUDE, MUTUAL) or rt_def:
            desc = (rt_def.get("description") if rt_def else "") or rule.get("description", "")
            if desc:
                ttk.Label(form, text="插件说明").grid(row=row, column=0, sticky="ne", padx=(0, 6), pady=4)
                ttk.Label(form, text=desc, style="Dim.TLabel", wraplength=theme.px(380), justify="left").grid(
                    row=row, column=1, columnspan=2, sticky="w", pady=4)
                row += 1

            fields_list = rt_def.get("fields", []) if rt_def else []
            if not fields_list:
                for k in rule.keys():
                    if k not in ("id", "name", "type", "enabled", "color", "plugin_id"):
                        fields_list.append({"key": k, "label": k, "default": ""})

            for f_spec in fields_list:
                k = f_spec.get("key")
                if not k:
                    continue
                label = f_spec.get("label") or k
                default_val = f_spec.get("default", "")
                val = rule.get(k, default_val)
                ttk.Label(form, text=label).grid(row=row, column=0, sticky="e", padx=(0, 6), pady=4)
                var = tk.StringVar(value=str(val if val is not None else ""))
                plugin_field_vars[k] = var
                e = ttk.Entry(form, textvariable=var, width=38)
                e.grid(row=row, column=1, columnspan=2, sticky="w", pady=4)
                var.trace_add("write", lambda *_: queue())
                row += 1

        err_label.grid(row=row, column=0, columnspan=3, sticky="w", pady=(8, 0))
        if rule.get("type") in (KEYWORD, LLM):
            form.rowconfigure(1, weight=1)  # 关键词列表 / AI 提示词占满剩余高度
        if rule.get("type") in (EXCLUDE, MUTUAL):
            form.rowconfigure(list_row, weight=1)  # 名单占满剩余高度
        form.columnconfigure(1, weight=1)

        def writeback():
            current = next((r for r in self._rules() if r.get("id") == rule_id), None)
            if current is None:
                return
            name = name_var.get().strip()
            if name:
                current["name"] = name
            current["enabled"] = bool(enabled_var.get())
            current["color"] = color_selected["v"]
            if plugin_field_vars:
                for k, var in plugin_field_vars.items():
                    current[k] = var.get().strip()
            if rule.get("type") == KEYWORD:
                current["keywords"] = list(keywords)
                if mode_var is not None:
                    current["mode"] = mode_var.get()
                current["fields"] = self._chosen_fields(field_vars)
                if not keywords:
                    err_label.config(text="还没有关键词，此规则暂不会命中任何玩家", foreground=WARN)
                else:
                    err_label.config(text="")
            elif rule.get("type") == REGEX:
                current["pattern"] = pattern_var.get()
                current["fields"] = self._chosen_fields(field_vars)
            elif rule.get("type") == LLM:
                p = prompt_text.get("1.0", "end").strip()
                if p:
                    current["prompt"] = p
                    err_label.config(text="")
                else:
                    err_label.config(text="判断要求不能为空（暂保留原内容）", foreground=WARN)
            elif rule.get("type") == EXCLUDE:
                current["exclude_self"] = bool(self_var.get())
                current["exclude_friends"] = bool(friends_var.get())
                current["users"] = list(users)
                if not (self_var.get() or friends_var.get() or users):
                    err_label.config(text="还没有选择任何排除条件，此规则不会排除任何人", foreground=WARN)
                else:
                    err_label.config(text="")
            elif rule.get("type") == MUTUAL:
                current["operator"] = OP_FROM_DISPLAY.get(op_var.get(), ">=")
                try:
                    current["count"] = int(count_var.get())
                    err_label.config(text="")
                except ValueError:
                    err_label.config(text="数量需为整数（暂保留原值 %s）" % current.get("count", 5),
                                     foreground=WARN)
                current["limit_users"] = bool(limit_users_var.get())
                current["users"] = list(mutual_users)
                if limit_users_var.get() and not mutual_users:
                    err_label.config(text="已开启名单筛选，但尚未添加任何目标玩家", foreground=WARN)
                elif not err_label.cget("text").startswith("数量需为整数"):
                    err_label.config(text="")
            self._refresh_tree_item(current)
            self.config.schedule_save()

        def queue(_=None):
            self._queue_wb(rule_id, writeback)

        def flush_now(_=None):
            self._queue_wb(rule_id, writeback)
            self._flush_wb()

        name_var.trace_add("write", lambda *_: queue())
        if rule.get("type") == REGEX:
            update_test()

    def _chosen_fields(self, field_vars):
        fields = [f for f in ALL_FIELDS if field_vars[f].get()]
        return fields or ["bio"]


class StatusTab(ttk.Frame):
    """状态页：表格显示当前实例内的所有人（含零命中的），及其检测状态与命中规则。"""

    STATUS_COLOR = {
        "已检测": FG,
        "检测中": theme.ACCENT,
        "等待检测": FG_DIM,
        "获取失败": ERR,
        "无法查询": FG_DIM,
        "本机": FG_DIM,
        "已排除": FG_DIM,
    }

    def __init__(self, parent, matcher):
        super().__init__(parent, padding=6)
        self.matcher = matcher
        self._row_ids = {}    # user_id -> tree item id（保持选中不闪烁用不上，简单重建）

        self.summary = ttk.Label(self, text="尚未进入实例", style="Dim.TLabel")
        self.summary.pack(fill="x", pady=(0, 6))

        columns = ("name", "status", "rules", "bio")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", selectmode="none")
        for col, text, width in (("name", "玩家", theme.px(140)),
                                 ("status", "状态", theme.px(90)),
                                 ("rules", "命中的规则", theme.px(240)),
                                 ("bio", "简介（限宽）", theme.px(240))):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("matched", background="#25384d")
        self.tree.tag_configure("local", foreground=FG_DIM)
        self.tree.tag_configure("failed", foreground=ERR)

        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        # Treeview 独占整行时滚动条要放同侧；改用 place 贴右缘
        scrollbar.place(relx=1.0, rely=0, anchor="ne", relheight=0.9)

        self.after(1000, self._poll)

    def _poll(self):
        try:
            self._refresh()
        except Exception:
            pass
        self.after(1000, self._poll)

    def _refresh(self):
        info = self.matcher.get_summary()
        parts = []
        if info["instance"]:
            parts.append("实例「%s」" % short_text(info["instance"], 24))
        else:
            parts.append("尚未进入实例")
        parts.append("玩家 %d 人" % info["players"])
        parts.append("已匹配 %d 人" % info["matched"])
        if info["scanning"]:
            parts.append("扫描中…")
        elif info["last_scan"]:
            parts.append("上次扫描 %s" % time.strftime("%H:%M:%S", time.localtime(info["last_scan"])))
        self.summary.config(text=" · ".join(parts))

        roster = self.matcher.get_roster()
        self.tree.delete(*self.tree.get_children())
        for entry in roster:
            matched = bool(entry["rules"])
            rules_text = "、".join(
                short_text(r["name"], 12) + ("(%s)" % short_text(r["reason"], 16) if r["reason"] else "")
                for r in entry["rules"]
            ) or "—"
            name = entry["name"] or "(未知)"
            status = entry["status"]
            bio = entry.get("bio", "") or "—"
            if status == "本机":
                name += "（你）"
            tags = []
            if matched:
                tags.append("matched")
            if status == "本机":
                tags.append("local")
            if status == "获取失败":
                tags.append("failed")
            self.tree.insert("", "end", values=(name, status, rules_text, bio), tags=tags)


class RulePickDialog(tk.Toplevel):
    """规则多选对话框（导入/导出共用）：默认全选，带全选/全不选。

    调用方用 wait_window 等待；关闭后 self.result 为选中的 Rule 列表，取消时为 None。
    """

    def __init__(self, parent, title, rules, confirm_text):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=BG)
        self.result = None
        # 注意：不设 transient —— 本程序的 root 是 withdrawn 的，transient 会让对话框跟着隐藏
        self.rules = list(rules)
        self.vars = [tk.BooleanVar(value=True) for _ in self.rules]

        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(fill="x")
        ttk.Button(bar, text="全选", style="Tool.TButton",
                   command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(bar, text="全不选", style="Tool.TButton",
                   command=lambda: self._set_all(False)).pack(side="left", padx=(6, 0))
        ttk.Label(bar, text="共 %d 条" % len(self.rules), style="Dim.TLabel").pack(
            side="left", padx=(12, 0))

        body = ttk.Frame(self, padding=(10, 0))
        body.pack(fill="both", expand=True)
        canvas = tk.Canvas(body, highlightthickness=0, bg=BG,
                           height=min(len(self.rules) * theme.px(30) + theme.px(10), theme.px(320)))
        scroll = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw", tags="inner")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure("inner", width=e.width))
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        for i, rule in enumerate(self.rules):
            d = rule.to_dict()
            text = "%s（%s）" % (d.get("name", ""), TYPE_LABEL.get(d.get("type"), ""))
            summary = rule_summary(d)
            if summary:
                text += "　" + summary
            ttk.Checkbutton(inner, text=text, variable=self.vars[i]).pack(
                fill="x", anchor="w", pady=1)

        btns = ttk.Frame(self, padding=(10, 8))
        btns.pack(fill="x", side="bottom")
        ttk.Button(btns, text="取消", command=self._cancel).pack(side="right")
        ttk.Button(btns, text=confirm_text, style="Accent.TButton",
                   command=self._confirm).pack(side="right", padx=(0, 8))

        self.bind("<Escape>", lambda e: self._cancel())
        self.grab_set()

    def _set_all(self, value):
        for v in self.vars:
            v.set(value)

    def _confirm(self):
        self.result = [r for r, v in zip(self.rules, self.vars) if v.get()]
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


class SettingsTab(ttk.Frame):
    def __init__(self, parent, config, matcher, apply_hooks, overlay=None, on_rules_changed=None):
        super().__init__(parent, padding=8)
        self.config = config
        self.matcher = matcher
        self.apply_hooks = list(apply_hooks)
        self.overlay = overlay
        self.on_rules_changed = on_rules_changed
        self.vars = {}
        self._apply_after = None
        self._loading = False

        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        canvas = tk.Canvas(outer, highlightthickness=0, bg=BG)
        self.canvas = canvas
        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        page = ttk.Frame(canvas)
        page.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=page, anchor="nw", tags="page")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.itemconfigure("page", width=620)
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure("page", width=e.width - 4))
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        outer.rowconfigure(0, weight=1)

        self._cookie_section(page, r=0)
        self._llm_section(page, r=1)
        self._scan_section(page, r=2)
        self._overlay_section(page, r=3)
        self._actions(page, r=4)

    def _section(self, page, r, title):
        box = ttk.Labelframe(page, text=title, padding=8)
        box.grid(row=r, column=0, sticky="we", pady=(0, 10))
        box.columnconfigure(1, weight=1)
        return box

    def _add_row(self, box, row, label, key, show="", width=36):
        """创建带即时写回的输入框：每键写入配置并去抖应用。"""
        ttk.Label(box, text=label).grid(row=row, column=0, sticky="e", padx=(0, 8), pady=3)
        var = tk.StringVar(value=str(self.config.get(*key, default="") or ""))
        ttk.Entry(box, textvariable=var, show=show, width=width).grid(row=row, column=1, sticky="we", pady=3)
        self.vars[key] = var
        var.trace_add("write", lambda *_: self._on_var(key))
        return var

    def _on_var(self, key):
        if self._loading:
            return
        var = self.vars[key]
        raw = var.get()
        if key in INT_KEYS:
            try:
                value = int(raw)
            except ValueError:
                self._hint("「%s」需为整数，暂保留原值" % self._key_label(key), WARN)
                return
        elif key in FLOAT_KEYS:
            try:
                value = float(raw)
            except ValueError:
                self._hint("「%s」需为数字，暂保留原值" % self._key_label(key), WARN)
                return
        else:
            value = raw
        self.config.set(value, *key, defer=True)
        self._schedule_apply()
        self._hint("更改即时生效")

    KEY_LABELS = {
        ("llm", "batch_size"): "batch_size",
        ("llm", "concurrency"): "并发数",
        ("api", "profile_cache_hours"): "简介缓存有效期",
        ("llm", "result_cache_max"): "LLM结果缓存上限",
        ("api", "requests_per_second"): "API 请求频率",
        ("scan", "debounce_seconds"): "进房防抖",
    }

    def _key_label(self, key):
        return self.KEY_LABELS.get(key, "/".join(key))

    def _schedule_apply(self):
        if self._apply_after:
            self.after_cancel(self._apply_after)
        self._apply_after = self.after(APPLY_DEBOUNCE_MS, self._apply_now)

    def _apply_now(self):
        self._apply_after = None
        for hook in self.apply_hooks:
            try:
                hook()
            except Exception as exc:
                self._hint("应用失败: %s" % exc, ERR)
                return

    def _export_rules(self):
        rules = [Rule.from_dict(r) for r in self.config.rules if isinstance(r, dict)]
        dlg = RulePickDialog(self, "导出规则：选择要导出的规则", rules, "导出…")
        self.wait_window(dlg)
        if dlg.result is None:
            return
        if not dlg.result:
            self._hint("未选择任何规则", WARN)
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="导出规则", defaultextension=".json",
            initialfile="rules-" + time.strftime("%Y%m%d") + ".json",
            filetypes=[("JSON 规则文件", "*.json"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(rules_payload([r.to_dict() for r in dlg.result]), f,
                          ensure_ascii=False, indent=2)
            self._hint("已导出 %d 条规则" % len(dlg.result), OK)
        except OSError as exc:
            self._hint("导出失败: %s" % exc, ERR)

    def _import_rules(self):
        path = filedialog.askopenfilename(
            parent=self, title="导入规则",
            filetypes=[("JSON 规则文件", "*.json"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            self._hint("导入失败：文件无法读取（%s）" % exc, ERR)
            return
        parsed, invalid = load_rules_payload(data)
        if not parsed:
            self._hint("文件中没有可导入的规则（无效 %d 条）" % invalid, WARN)
            return
        dlg = RulePickDialog(self, "导入规则：选择要导入的规则", parsed, "导入…")
        self.wait_window(dlg)
        if dlg.result is None:
            return
        if not dlg.result:
            self._hint("未选择任何规则", WARN)
            return

        existing = self.config.rules
        used_ids = set()
        existing_sigs = set()
        for r in existing:
            rule = Rule.from_dict(r)
            used_ids.add(rule.id)
            existing_sigs.add(rule_signature(rule))
        imported, skipped = 0, 0
        for rule in dlg.result:
            sig = rule_signature(rule)
            if sig in existing_sigs:
                skipped += 1   # 与现有规则内容完全相同，去重跳过
                continue
            if rule.id in used_ids:
                rule.id = self.config.new_rule_id()  # id 撞车但内容不同 -> 换新 id
            existing.append(rule.to_dict())
            used_ids.add(rule.id)
            existing_sigs.add(sig)
            imported += 1
        if imported:
            self.config.set(existing, "rules")
            if self.on_rules_changed:
                self.on_rules_changed()
        self._hint("导入 %d 条，跳过重复 %d 条" % (imported, skipped), OK if imported else WARN)

    def _hint(self, text, color=FG_DIM):
        self.status_label.config(text=text, foreground=color)

    def _cookie_section(self, page, r):
        box = self._section(page, r, "VRChat Cookie（浏览器登录 vrchat.com 后 F12 → 应用 → Cookie 复制）")
        self._add_row(box, 0, "auth", ("cookies", "auth"), show="*")
        self._add_row(box, 1, "twoFactorAuth（可留空）", ("cookies", "twoFactorAuth"), show="*")
        self.cookie_result = ttk.Label(box, text="")
        self.cookie_result.grid(row=2, column=1, sticky="w", pady=(2, 0))
        btn(box, "测试 Cookie", self._test_cookie).grid(row=2, column=0, sticky="e", pady=(2, 0))

    def _llm_section(self, page, r):
        box = self._section(page, r, "大模型（OpenAI 兼容接口，仅 AI 类规则需要）")
        self._add_row(box, 0, "base_url", ("llm", "base_url"))
        self._add_row(box, 1, "api_key", ("llm", "api_key"), show="*")
        self._add_row(box, 2, "model", ("llm", "model"), width=20)
        self._add_row(box, 3, "batch_size（每次请求带几个人）", ("llm", "batch_size"), width=8)
        self._add_row(box, 4, "并发数", ("llm", "concurrency"), width=8)
        self._add_row(box, 5, "LLM结果缓存上限（条，同简介+同规则不重复调用，超限淘汰最久未用）",
                      ("llm", "result_cache_max"), width=8)

    def _scan_section(self, page, r):
        box = self._section(page, r, "扫描")
        self._add_row(box, 0, "API 请求频率（次/秒）", ("api", "requests_per_second"), width=8)
        self._add_row(box, 1, "进房防抖（秒）", ("scan", "debounce_seconds"), width=8)
        self._add_row(box, 2, "玩家简介缓存有效期（小时，过期后重新拉取资料）",
                      ("api", "profile_cache_hours"), width=8)

    def _overlay_section(self, page, r):
        box = self._section(page, r, "悬浮窗")
        ttk.Label(box, text="透明度").grid(row=0, column=0, sticky="e", padx=(0, 8), pady=3)
        alpha = float(self.config.get("ui", "alpha", default=0.9) or 0.9)
        self.alpha_var = tk.DoubleVar(value=alpha)
        scale = ttk.Scale(box, from_=0.5, to=1.0, variable=self.alpha_var, orient="horizontal")
        scale.grid(row=0, column=1, sticky="we", pady=3)
        self.alpha_label = ttk.Label(box, text="%d%%" % round(alpha * 100))
        self.alpha_label.grid(row=0, column=2, padx=(8, 0))
        self.alpha_var.trace_add("write", lambda *_: self._on_alpha())

        self.vars[("ui", "show_avatars")] = tk.BooleanVar(
            value=bool(self.config.get("ui", "show_avatars", default=True)))
        ttk.Checkbutton(box, text="显示头像", variable=self.vars[("ui", "show_avatars")],
                        command=self._on_flag).grid(row=1, column=1, sticky="w", pady=3)
        self.vars[("ui", "always_on_top")] = tk.BooleanVar(
            value=bool(self.config.get("ui", "always_on_top", default=True)))
        ttk.Checkbutton(box, text="窗口置顶", variable=self.vars[("ui", "always_on_top")],
                        command=self._on_flag).grid(row=2, column=1, sticky="w", pady=3)

        ttk.Label(box, text="单列最大行数").grid(row=3, column=0, sticky="e", padx=(0, 8), pady=3)
        row_box = ttk.Frame(box)
        row_box.grid(row=3, column=1, sticky="w", pady=3)
        max_rows_val = str(self.config.get("ui", "overlay_max_rows", default=20) or 20)
        self.vars[("ui", "overlay_max_rows")] = tk.StringVar(value=max_rows_val)
        spin = ttk.Spinbox(row_box, from_=3, to=100, width=6,
                           textvariable=self.vars[("ui", "overlay_max_rows")],
                           command=self._on_max_rows)
        spin.pack(side="left")
        self.vars[("ui", "overlay_max_rows")].trace_add("write", lambda *_: self._on_max_rows())
        ttk.Label(row_box, text="行（超出后自动分列显示）").pack(side="left", padx=(6, 0))

    def _on_alpha(self, _value=None):
        if self._loading:
            return
        alpha = round(min(1.0, max(0.5, float(self.alpha_var.get()))), 2)
        self.alpha_label.config(text="%d%%" % round(alpha * 100))
        self.config.set(alpha, "ui", "alpha", defer=True)
        if self.overlay is not None:
            self.overlay.set_alpha(alpha)

    def _on_flag(self):
        if self._loading:
            return
        self.config.set(bool(self.vars[("ui", "show_avatars")].get()), "ui", "show_avatars")
        self.config.set(bool(self.vars[("ui", "always_on_top")].get()), "ui", "always_on_top")
        self._schedule_apply()
        self._hint("更改即时生效")

    def _on_max_rows(self):
        if self._loading:
            return
        try:
            val = int(self.vars[("ui", "overlay_max_rows")].get())
            val = max(1, min(200, val))
        except (ValueError, TypeError):
            return
        self.config.set(val, "ui", "overlay_max_rows")
        self._schedule_apply()
        self._hint("更改即时生效")

    def refresh(self):
        """打开主面板时从配置回读一遍（悬浮窗菜单里改过的透明度等会同步到控件）。"""
        self._loading = True
        try:
            for key, var in self.vars.items():
                value = self.config.get(*key, default=None)
                if value is None:
                    continue
                if isinstance(var, tk.BooleanVar):
                    var.set(bool(value))
                else:
                    var.set(str(value))
            alpha = float(self.config.get("ui", "alpha", default=0.9) or 0.9)
            self.alpha_var.set(alpha)
            self.alpha_label.config(text="%d%%" % round(alpha * 100))
        finally:
            self._loading = False

    def _actions(self, page, r):
        box = ttk.Frame(page)
        box.grid(row=r, column=0, sticky="we")
        self.status_label = ttk.Label(box, text="所有更改即时生效，自动保存")
        self.status_label.pack(side="left", padx=(10, 0))
        btn(box, "清空 LLM 缓存", self._clear_llm).pack(side="right")
        btn(box, "清空简介缓存", self._clear_profiles).pack(side="right", padx=(0, 8))
        btn(box, "导入规则", self._import_rules).pack(side="right", padx=(0, 8))
        btn(box, "导出规则", self._export_rules).pack(side="right", padx=(0, 8))

    def _test_cookie(self):
        from vrchat_api import VRChatAPI

        self.cookie_result.config(text="测试中…", foreground=FG_DIM)

        def work():
            api = VRChatAPI(self.vars[("cookies", "auth")].get().strip(),
                            self.vars[("cookies", "twoFactorAuth")].get().strip())
            try:
                me = api.get_me()
                self.cookie_result.after(0, lambda: self.cookie_result.config(
                    text="有效，当前用户：%s" % me.get("displayName", "?"), foreground=OK))
            except Exception as exc:
                self.cookie_result.after(0, lambda: self.cookie_result.config(
                    text=str(exc), foreground=ERR))

        threading.Thread(target=work, daemon=True).start()

    def _clear_llm(self):
        self.matcher.cache.clear_llm()
        self._hint("LLM 缓存已清空", OK)

    def _clear_profiles(self):
        self.matcher.cache.clear_profiles()
        self._hint("简介缓存已清空", OK)


class PluginsTab(ttk.Frame):
    """插件管理选项卡：管理内置插件与外部插件（启用/禁用、热重载、目录直达）。"""
    def __init__(self, parent, matcher, config, on_plugins_changed=None):
        super().__init__(parent)
        self.matcher = matcher
        self.config = config
        self.on_plugins_changed = on_plugins_changed

        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        self._build_toolbar()
        self._build_table()
        self._build_details()
        self._reload_tree()

    def _build_toolbar(self):
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))

        btn(bar, "🔄 重新加载插件", self._reload_plugins).pack(side="left")
        btn(bar, "切换启用/禁用", self._toggle_selected).pack(side="left", padx=(8, 0))

        btn(bar, "📂 打开开发目录", self._open_dev_dir).pack(side="right")
        btn(bar, "📂 打开用户目录", self._open_user_dir).pack(side="right", padx=(0, 8))

    def _build_table(self):
        table_frame = ttk.Frame(self)
        table_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 6))
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        cols = ("enabled", "name", "id", "version", "source", "status")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("enabled", text="启用")
        self.tree.heading("name", text="插件名称")
        self.tree.heading("id", text="标识符 (ID)")
        self.tree.heading("version", text="版本")
        self.tree.heading("source", text="来源")
        self.tree.heading("status", text="状态")

        self.tree.column("enabled", width=50, anchor="center")
        self.tree.column("name", width=180, anchor="w")
        self.tree.column("id", width=140, anchor="w")
        self.tree.column("version", width=70, anchor="center")
        self.tree.column("source", width=140, anchor="center")
        self.tree.column("status", width=120, anchor="w")

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-Button-1>", lambda e: self._toggle_selected())

    def _build_details(self):
        self.detail_frame = ttk.LabelFrame(self, text="插件详情", padding=8)
        self.detail_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))

        self.lbl_name = ttk.Label(self.detail_frame, text="", font=(theme.FONT, 10, "bold"))
        self.lbl_name.pack(anchor="w")

        self.lbl_desc = ttk.Label(self.detail_frame, text="请从上方列表选择插件", wraplength=700, style="Dim.TLabel")
        self.lbl_desc.pack(anchor="w", pady=(4, 2))

        self.lbl_path = ttk.Label(self.detail_frame, text="", style="Dim.TLabel")
        self.lbl_path.pack(anchor="w")

    def _reload_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        if not self.matcher or not hasattr(self.matcher, "plugin_manager"):
            return

        source_labels = {
            "builtin": "内置插件",
            "dev": "开发目录 (plugins_dev)",
            "user": "用户目录 (AppData)",
        }
        plugins = self.matcher.plugin_manager.get_all_plugins()
        for p in plugins:
            status_text = "正常" if not p.error else f"错误: {p.error}"
            source_text = source_labels.get(p.source, p.source)
            item_id = self.tree.insert("", "end", values=(
                "✓" if p.enabled else "×",
                p.name,
                p.id,
                p.version,
                source_text,
                status_text,
            ))
            self.tree.item(item_id, tags=(p.id,))

    def _on_select(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        item_id = sel[0]
        plugin_id = self.tree.item(item_id, "tags")[0]
        if self.matcher and hasattr(self.matcher, "plugin_manager"):
            p = self.matcher.plugin_manager.plugins.get(plugin_id)
            if p:
                self.lbl_name.config(text=f"{p.name} (v{p.version})")
                desc = p.description or "(暂无描述)"
                if p.author:
                    desc += f"  ·  作者: {p.author}"
                if p.error:
                    desc += f"\n[加载异常]: {p.error}"
                self.lbl_desc.config(text=desc)
                self.lbl_path.config(text=f"目录: {p.path}" if p.path else "内置引擎插件")

    def _toggle_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        item_id = sel[0]
        plugin_id = self.tree.item(item_id, "tags")[0]
        if self.matcher and hasattr(self.matcher, "plugin_manager"):
            p = self.matcher.plugin_manager.plugins.get(plugin_id)
            if p:
                new_state = not p.enabled
                self.matcher.plugin_manager.set_plugin_enabled(plugin_id, new_state)
                self.tree.set(item_id, "enabled", "✓" if new_state else "×")
                if self.on_plugins_changed:
                    self.on_plugins_changed()

    def _reload_plugins(self):
        if self.matcher and hasattr(self.matcher, "plugin_manager"):
            self.matcher.plugin_manager.reload()
            self._reload_tree()
            if self.on_plugins_changed:
                self.on_plugins_changed()

    def _open_dev_dir(self):
        if self.matcher and hasattr(self.matcher, "plugin_manager"):
            path = self.matcher.plugin_manager.dev_plugins_dir
            os.makedirs(path, exist_ok=True)
            try:
                os.startfile(path)
            except Exception:
                pass

    def _open_user_dir(self):
        if self.matcher and hasattr(self.matcher, "plugin_manager"):
            path = self.matcher.plugin_manager.user_plugins_dir
            os.makedirs(path, exist_ok=True)
            try:
                os.startfile(path)
            except Exception:
                pass
