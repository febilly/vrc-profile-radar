"""VRChat Profile 雷达 —— 入口。

用法:
  python main.py                  # 悬浮窗 + 主面板
  python main.py --console        # 无界面，结果打印到控制台
  python main.py --demo           # 演示模式：假数据跑完整流程（可不填 Cookie）
  python main.py --demo --console # 演示模式 + 控制台输出
  python main.py --check          # 运行自检测试
  python main.py --clear-llm-cache / --clear-profile-cache
"""
import argparse
import sys
import time

import config as config_mod
from cache_store import CacheStore
from matcher import Matcher
from vrchat_api import VRChatAPI
from vrchat_log import LogTailer


def build_api(cfg):
    return VRChatAPI(
        cfg.auth_cookie,
        cfg.two_factor_cookie,
        requests_per_second=cfg.get("api", "requests_per_second", default=2.0) or 2.0,
    )


def run_console(matcher):
    last = {}

    def sink(event):
        etype = event.get("type")
        if etype == "status":
            text = event.get("text") or ""
            if text:
                print("[状态] %s" % text)
        elif etype == "matches":
            current = {}
            for match in event.get("matches", []):
                key = tuple((r["id"], r.get("reason", "")) for r in match.get("rules", []))
                current[match["user_id"]] = (match.get("name", ""), key)
            for uid, (name, _key) in current.items():
                if last.get(uid) != (name, key):
                    rules = "、".join(
                        "%s(%s)" % (r["name"], r.get("reason", ""))
                        for m in event.get("matches", []) if m["user_id"] == uid
                        for r in m.get("rules", [])
                    )
                    print("[匹配] %-20s ← %s" % (name, rules))
            for uid, (name, _key) in last.items():
                if uid not in current:
                    print("[离开] %s" % name)
            last.clear()
            last.update(current)

    matcher.sinks.append(sink)
    print("控制台模式运行中，Ctrl+C 退出。")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        matcher.stop()
        print("已退出。")


def run_gui(cfg, cache, matcher, demo):
    import tkinter as tk
    import theme
    from overlay import Overlay
    from main_window import MainWindow

    theme.enable_high_dpi()          # 必须在创建任何窗口之前
    root = tk.Tk()
    root.withdraw()
    root._demo = demo
    theme.init_scaling(root)
    theme.apply_dark_style(root)

    def rebuild_api():
        matcher.set_api(build_api(cfg))

    def quit_app():
        root.destroy()               # 悬浮窗/主面板都是它的子窗口，一并关闭

    overlay = Overlay(root, cfg, matcher, open_main=lambda: window.show(),
                      on_quit=quit_app)
    window = MainWindow(root, cfg, matcher, apply_hooks=[
        rebuild_api,
        matcher.reload_settings,
        overlay.apply_settings,
    ], overlay=overlay, on_quit=quit_app)
    matcher.start()
    root.mainloop()
    matcher.stop()


def main():
    parser = argparse.ArgumentParser(description="VRChat Profile 雷达")
    parser.add_argument("--demo", action="store_true", help="演示模式（假数据）")
    parser.add_argument("--console", action="store_true", help="无界面控制台模式")
    parser.add_argument("--check", action="store_true", help="运行自检测试")
    parser.add_argument("--config", help="config.json 路径")
    parser.add_argument("--log-dir", help="覆盖 VRChat 日志目录")
    parser.add_argument("--clear-llm-cache", action="store_true")
    parser.add_argument("--clear-profile-cache", action="store_true")
    args = parser.parse_args()

    if args.check:
        import selfcheck
        sys.exit(0 if selfcheck.run() else 1)

    cfg = config_mod.Config(args.config)
    cache = CacheStore(cfg.cache_path)
    if args.clear_llm_cache:
        cache.clear_llm()
        print("LLM 缓存已清空")
    if args.clear_profile_cache:
        cache.clear_profiles()
        print("资料缓存已清空")

    matcher = Matcher(cfg, cache, api=build_api(cfg))

    if args.demo:
        import demo
        demo.setup(matcher, cfg, console=args.console, include_plugin_demo=True)
    else:
        # log_dir 留空 = 自动搜索新版/旧版两个日志位置
        log_dir = args.log_dir or cfg.get("log_dir", default="") or None
        tailer = LogTailer(matcher.handle_log_event, log_dir=log_dir)
        tailer.start()
        if not args.console:
            print("监控日志位置: 自动（新版 VRChat 根目录 + 旧版 Output Log 子目录）")

    if args.console:
        run_console(matcher)
        return

    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("未安装 tkinter，退回控制台模式。")
        run_console(matcher)
        return

    run_gui(cfg, cache, matcher, demo=args.demo)


if __name__ == "__main__":
    main()
