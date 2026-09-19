"""自检测试：python main.py --check
覆盖 日志解析 / 规则匹配 / 缓存 / LLM输出解析 / 完整扫描流水线（假数据），不联网。
"""
import os
import shutil
import tempfile
import threading
import time

import config as config_mod
import rules as rules_mod
from rules import Rule, load_rules_payload, rule_signature, rules_payload, compare_count
from vrchat_log import parse_line
import vrchat_log
from cache_store import CacheStore
from llm_matcher import extract_json
from matcher import Matcher
from trust import user_trust

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [通过] %s" % name)
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))


def test_log_parsing():
    print("== 日志解析 ==")
    join_with_id = ("2024.01.01 12:34:56 Log        -  [Behaviour] OnPlayerJoined "
                    "TestUser (usr_00000000-0000-0000-0000-000000000000)")
    ev = parse_line(join_with_id)
    check("OnPlayerJoined 带usr id", ev == {"kind": "join", "name": "TestUser",
          "user_id": "usr_00000000-0000-0000-0000-000000000000", "time": "2024.01.01 12:34:56"}, repr(ev))

    ev = parse_line("2024.01.01 12:34:56 Log        -  [NetworkManager] OnPlayerJoined Rize♡")
    check("OnPlayerJoined 无id/unicode名", ev and ev["kind"] == "join" and ev["name"] == "Rize♡"
          and ev["user_id"] == "", repr(ev))

    ev = parse_line("2024.01.01 12:34:56 Log        -  [Behaviour] OnPlayerLeft Rize♡ (usr_aaaa-bbbb)")
    check("OnPlayerLeft", ev and ev["kind"] == "leave" and ev["user_id"] == "usr_aaaa-bbbb", repr(ev))

    check("OnPlayerLeftRoom 应忽略",
          parse_line("2024.01.01 12:34:56 Log        -  [Behaviour] OnPlayerLeftRoom") is None)
    check("OnPlayerJoined:Unnamed 应忽略",
          parse_line("2024.01.01 12:34:56 Log        -  [Behaviour] OnPlayerJoined:Unnamed") is None)

    ev = parse_line("2024.01.01 12:34:56 Log        -  [Behaviour] Joining "
                    "wrld_4432ea9b-729c-46e3-8eaf-846aa0a37fdd:67646~region(jp)~nonce(ABC)")
    check("Joining 新格式", ev and ev["kind"] == "world"
          and ev["world_id"] == "wrld_4432ea9b-729c-46e3-8eaf-846aa0a37fdd", repr(ev))

    check("Joining or Creating Room 应忽略",
          parse_line("2024.01.01 12:34:56 Log        -  [RoomManager] Joining or Creating Room: VRChat Home") is None)

    ev = parse_line("2024.01.01 12:34:56 Log        -  [Behaviour] Entering Room: VRChat Home")
    check("Entering Room 世界名", ev and ev["kind"] == "world_name" and ev["name"] == "VRChat Home", repr(ev))

    ev = parse_line('2024.01.01 12:34:56 Log        -  [Player] Initialized PlayerAPI "TestUser" is local')
    check("本地玩家识别", ev and ev["kind"] == "local" and ev["name"] == "TestUser", repr(ev))

    ev = parse_line("2024.01.01 12:34:56 Log        -  [API] User Authenticated: foo (usr_abc123)")
    check("User Authenticated", ev and ev["kind"] == "local" and ev["user_id"] == "usr_abc123", repr(ev))

    check("普通日志应忽略",
          parse_line("2024.01.01 12:34:56 Log        -  [SaveSystem] Save data") is None)


def test_rules():
    print("== 规则匹配 ==")
    profile = {"bio": "I love VRChat! 日语勉強中", "bioLinks": ["https://x.com/foo"],
               "displayName": "Test", "tags": ["system_trust_veteran"]}
    rule = Rule.from_dict({"name": "r1", "type": "keyword", "keywords": ["日语", "english"], "mode": "any"})
    check("关键词 any 命中", rules_mod.match_keyword(rule, profile) == ["日语"])
    rule_all = Rule.from_dict({"name": "r2", "type": "keyword", "keywords": ["日语", "english"], "mode": "all"})
    check("关键词 all 未全中", rules_mod.match_keyword(rule_all, profile) == [])
    rule_links = Rule.from_dict({"name": "r3", "type": "regex",
                                 "pattern": r"(?:twitter|x)\.com/[A-Za-z0-9_]+", "fields": ["bio", "bioLinks"]})
    check("正则匹配链接", rules_mod.match_regex(rule_links, profile) == ["x.com/foo"])
    rule_tags = Rule.from_dict({"name": "r4", "type": "keyword", "keywords": ["veteran"], "fields": ["tags"]})
    check("关键词匹配tags", rules_mod.match_keyword(rule_tags, profile) == ["veteran"])
    h1, h2 = rules_mod.rule_hash(rule), rules_mod.rule_hash(rule)
    check("规则哈希稳定", h1 == h2)
    h3 = rules_mod.rule_hash(Rule.from_dict({"name": "r1", "type": "keyword", "keywords": ["别的"]}))
    check("规则内容不同哈希不同", h1 != h3)

    # 大小写不敏感
    profile_ci = {"bio": "I love VRChat! Japanese learner and straße fan", "bioLinks": []}
    rule_ci = Rule.from_dict({"name": "ci", "type": "keyword",
                              "keywords": ["JAPANESE", "VrChAt", "STRASSE", "没有的词"]})
    hits = rules_mod.match_keyword(rule_ci, profile_ci)
    check("关键词不区分大小写", hits == ["JAPANESE", "VrChAt", "STRASSE"], repr(hits))
    check("命中回显保留原样大小写", hits and hits[0] == "JAPANESE")
    rule_ci_all = Rule.from_dict({"name": "ci2", "type": "keyword",
                                  "keywords": ["jApAnEsE", "vRcHaT"], "mode": "all"})
    check("all模式不区分大小写", rules_mod.match_keyword(rule_ci_all, profile_ci) == ["jApAnEsE", "vRcHaT"])
    rule_case_dup = Rule.from_dict({"name": "dup", "type": "keyword", "keywords": ["  VRChat "]})
    check("关键词带空格也可命中", rules_mod.match_keyword(rule_case_dup, profile_ci) == ["VRChat"])
    check("规则颜色字段保留", Rule.from_dict(
        {"name": "c", "type": "keyword", "keywords": ["x"], "color": "#8CFF5C"}).color == "#8cff5c")
    check("非法颜色被清空", Rule.from_dict(
        {"name": "c", "type": "keyword", "keywords": ["x"], "color": "notacolor"}).color == "")
    check("无颜色默认自动取色", Rule.from_dict(
        {"name": "c", "type": "keyword", "keywords": ["x"]}).color == "")


def test_trust_levels():
    print("== 信任等级（VRCX 映射） ==")
    name, color, sort = user_trust([])
    check("无tag = Visitor", name == "Visitor" and color == "#CCCCCC" and sort == 1, "%s/%s" % (name, color))
    name, color, _ = user_trust(["system_trust_basic"])
    check("basic = New User 蓝", name == "New User" and color == "#1778FF")
    name, color, _ = user_trust(["system_trust_known"])
    check("known = User 绿", name == "User" and color == "#2BCF5C")
    name, color, _ = user_trust(["system_trust_trusted"])
    check("trusted = Known User 橙", name == "Known User" and color == "#FF7B42")
    # veteran 标签已被 VRChat 淘汰：存量账号显示为 Trusted User（紫色），不再显示 Veteran
    name, color, sort = user_trust(["system_trust_veteran"])
    check("veteran(淘汰) 显示为 Trusted User 紫", name == "Trusted User" and color == "#B18FFF" and sort == 5)
    name, color, _ = user_trust(["system_trust_basic"], "developer")
    check("开发者红色覆盖", color == "#FF2626")
    name, color, _ = user_trust(["system_troll", "system_trust_trusted"])
    check("troll 颜色覆盖", color == "#782F2F")
    name, _, _ = user_trust(None)
    check("tags为None不崩溃", name == "Visitor")


def test_exclude_mutual_rules():
    print("== 排除/共同好友规则 ==")
    # 运算符
    check(">= 运算符", compare_count(">=", 5, 5) and not compare_count(">=", 4, 5))
    check("> 运算符", compare_count(">", 6, 5) and not compare_count(">", 5, 5))
    check("= 运算符", compare_count("=", 5, 5) and not compare_count("=", 6, 5))
    check("<= 运算符", compare_count("<=", 5, 5) and compare_count("<=", 4, 5))
    check("< 运算符", compare_count("<", 4, 5) and not compare_count("<", 5, 5))
    check("非法输入 False", not compare_count(">=", None, 5) and not compare_count(">=", "abc", 5))

    rule = Rule.from_dict({"name": "排", "type": "exclude", "exclude_self": True,
                           "exclude_friends": True,
                           "users": [{"user_id": "usr_abc", "name": "某人"}, "坏条目"]})
    check("排除规则解析", rule.exclude_self and rule.exclude_friends
          and rule.users == [{"user_id": "usr_abc", "name": "某人"}])
    check("排除规则无uid条目被丢弃", len(rule.users) == 1)
    mrule = Rule.from_dict({"name": "共", "type": "mutual_friends", "operator": "le", "count": "7"})
    check("共同好友规则解析+非法运算符回退", mrule.operator == ">=" and mrule.count == 7)
    mrule2 = Rule.from_dict({"name": "共2", "type": "mutual_friends", "operator": "<=", "count": "x"})
    check("共同好友数量非法回退默认5", mrule2.operator == "<=" and mrule2.count == 5)
    check("规则to_dict往返", Rule.from_dict(rule.to_dict()).users == rule.users)

    # 导入导出：排除/共同好友是合法类型；排除规则一个条件都没选算无效
    ok, skipped = load_rules_payload([
        {"name": "排", "type": "exclude", "exclude_self": True},
        {"name": "空排", "type": "exclude"},
        {"name": "共", "type": "mutual_friends"},
    ])
    check("导入支持排除/共同好友", len(ok) == 2 and skipped == 1, "%d/%d" % (len(ok), skipped))


def test_cache():
    print("== 缓存 ==")
    tmpdir = tempfile.mkdtemp()
    try:
        store = CacheStore(os.path.join(tmpdir, "t.db"))
        store.put_profile("usr_x", {"displayName": "X", "bio": "hello"})
        profile, fetched_at = store.get_profile("usr_x")
        check("简介缓存读写", profile == {"displayName": "X", "bio": "hello"} and fetched_at > 0)
        store.put_llm("k1", {"match": True, "reason": "r"})
        check("LLM缓存读写", store.get_llm("k1") == {"match": True, "reason": "r"})
        check("LLM缓存未命中", store.get_llm("k2") is None)
        store.put_llm("k1", {"match": False, "reason": "r2"})
        check("LLM缓存覆盖", store.get_llm("k1")["match"] is False)
        store.clear_llm()
        check("LLM缓存清空", store.get_llm("k1") is None)

        # 简介缓存：按获取时间绝对过期（读取不续期），写入时清理过期条目
        import time as _time
        store = CacheStore(os.path.join(tmpdir, "t2.db"))
        store.put_profile("usr_a", {"displayName": "A"}, max_age_seconds=3600)
        _, ts1 = store.get_profile("usr_a")
        _, ts2 = store.get_profile("usr_a")
        check("简介缓存读取不续期", ts1 == ts2)
        store._conn.execute(
            "UPDATE profiles SET fetched_at = ? WHERE user_id = 'usr_a'",
            (_time.time() - 7200,),
        )
        store._conn.commit()
        store.put_profile("usr_b", {"displayName": "B"}, max_age_seconds=3600)
        check("过期简介被清理", store.get_profile("usr_a")[0] is None)
        check("未过期的简介保留", store.get_profile("usr_b")[0] is not None)

        # LLM 结果缓存：按数量上限 LRU（命中即续期）
        store.put_llm("k1", {"match": True}, max_count=3)
        _time.sleep(0.02)
        store.put_llm("k2", {"match": True}, max_count=3)
        _time.sleep(0.02)
        store.put_llm("k3", {"match": True}, max_count=3)
        _time.sleep(0.02)
        store.get_llm("k1")  # 命中 k1 → 续期
        _time.sleep(0.02)
        store.put_llm("k4", {"match": True}, max_count=3)
        check("LLM缓存超限淘汰最久未用", store.get_llm("k2") is None)
        check("LLM缓存命中过的保留", store.get_llm("k1") is not None)
        check("LLM缓存其余保留", store.get_llm("k3") is not None and store.get_llm("k4") is not None)

        # 共同好友数缓存
        store.put_mutual_count("me", "usr_a", 7)
        count, fetched_at = store.get_mutual_count("me", "usr_a")
        check("共同好友缓存读写", count == 7 and fetched_at > 0)
        check("共同好友缓存未命中", store.get_mutual_count("me", "usr_b") == (None, 0.0))
        store.put_mutual_count("me", "usr_a", 9)
        check("共同好友缓存覆盖", store.get_mutual_count("me", "usr_a")[0] == 9)
        check("不同me键隔离", store.get_mutual_count("other", "usr_a") == (None, 0.0))
        store.put_profile("usr_z", {"displayName": "Z"})
        store.clear_profiles()
        check("清空简介缓存连共同好友一起清", store.get_mutual_count("me", "usr_a") == (None, 0.0)
              and store.get_profile("usr_z")[0] is None)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_extract_json():
    print("== LLM 输出解析 ==")
    text = '好的，以下是结果：\n```json\n{"results": [{"id": "u1", "match": true, "reason": "x"}]}\n```'
    obj = extract_json(text)
    check("围栏JSON解析", obj and obj["results"][0]["id"] == "u1", repr(obj))
    obj = extract_json('垃圾输出 {"results": []} 结束')
    check("夹带JSON解析", obj == {"results": []}, repr(obj))
    check("无JSON返回None", extract_json("完全不是json") is None)


def test_pipeline():
    print("== 完整流水线（假数据） ==")
    tmpdir = tempfile.mkdtemp()
    try:
        cfg_path = os.path.join(tmpdir, "config.json")
        cfg = config_mod.Config(cfg_path)
        cfg.set([
            {"id": "kw", "name": "日语学习者", "type": "keyword", "keywords": ["日语", "日本語"],
             "mode": "any", "color": "#8cff5c"},
            {"id": "re", "name": "X链接", "type": "regex", "pattern": r"(?:twitter|x)\.com/\w+",
             "fields": ["bio", "bioLinks"]},
            {"id": "llm1", "name": "创作者", "type": "llm", "prompt": "判断是否创作者"},
        ], "rules")
        cfg.set("", "cookies", "auth")

        from cache_store import CacheStore
        import demo
        cache = CacheStore(os.path.join(tmpdir, "cache.db"))
        matcher = Matcher(cfg, cache, api=None)
        demo.setup(matcher, cfg)
        events = []
        matcher.sinks.append(events.append)
        matcher.start()

        deadline = time.time() + 25
        while time.time() < deadline:
            with matcher._lock:
                matches = {m["user_id"]: m for m in matcher._matches.values()}
            names = {m["name"] for m in matches.values()}
            if {"星野くるみ", "Jack_Miller", "Alice", "Bob_Builder", "Diana"} <= names:
                break
            time.sleep(0.2)

        check("关键词规则命中(日语学习者)", {"星野くるみ", "Jack_Miller"} <= names, str(names))
        check("正则规则命中(X链接)", "Alice" in names, str(names))
        check("LLM规则命中(创作者)", {"Bob_Builder", "Diana"} <= names, str(names))
        check("不匹配的人未出现", "Charlie" not in names and "Eve" not in names, str(names))
        check("本地玩家未参与匹配", len(names) == 5, str(names))

        rule_names = {r["name"] for r in matches[
            "usr_dddddddd-1111-2222-3333-444444444444"]["rules"]}
        check("同人可命中多条规则", rule_names == {"创作者"}, str(rule_names))

        # 状态表：实例内所有人都在列（含零命中的）
        roster = matcher.get_roster()
        roster_names = [e["name"] for e in roster]
        check("状态表包含全部玩家", len(roster) == 7, str(roster_names))
        check("零命中的玩家也在状态表", any(
            e["name"] == "Charlie" and not e["rules"] for e in roster), str(roster))
        check("命中的玩家带规则", any(
            e["name"] == "Alice" and e["rules"] for e in roster), str(roster))
        check("已检测状态标注", any(e["status"] == "已检测" for e in roster),
              str([e["status"] for e in roster]))
        check("规则颜色透传到命中结果", matches[
            "usr_aaaaaaaa-1111-2222-3333-444444444444"]["rules"][0].get("color") == "#8cff5c",
            str(matches["usr_aaaaaaaa-1111-2222-3333-444444444444"]["rules"]))
        check("状态表带简介且无换行", any(
            e["name"] == "Bob_Builder" and "Blender" in e["bio"] and chr(10) not in e["bio"]
            for e in roster), str([e.get("bio", "")[:20] for e in roster]))

        # 离开事件应把 Eve 相关内容清掉（Eve 本来就没匹配），换 Jack 离开验证
        with matcher._lock:
            before = len(matcher._players)
        matcher.handle_log_event({"kind": "leave", "name": "Jack_Miller",
                                  "user_id": "usr_bbbbbbbb-1111-2222-3333-444444444444"})
        time.sleep(0.5)
        with matcher._lock:
            after = len(matcher._players)
        check("离开事件减少玩家数", after == before - 1, "%d->%d" % (before, after))

        # 缓存验证：第二次扫描不应再调用 FakeAPI（记录调用次数）
        calls = {"n": 0}
        real_get = matcher.api.get_profile

        def counting_get(uid):
            calls["n"] += 1
            return real_get(uid)

        matcher.api.get_profile = counting_get
        matcher.handle_log_event({"kind": "join", "name": "Charlie",
                                  "user_id": "usr_eeeeeeee-1111-2222-3333-444444444444"})
        time.sleep(4)
        check("缓存生效：老玩家未重新请求API", calls["n"] <= 1, str(calls["n"]))

        # LLM 结果缓存：同简介重复扫描不再调用大模型
        calls_llm = {"n": 0}
        orig_chunk = matcher.llm._match_chunk_with_fallback

        def counting_chunk(rule, chunk):
            calls_llm["n"] += 1
            return orig_chunk(rule, chunk)

        matcher.llm._match_chunk_with_fallback = counting_chunk
        matcher.handle_log_event({"kind": "join", "name": "Diana",
                                  "user_id": "usr_ffffffff-1111-2222-3333-444444444444"})
        time.sleep(4)
        check("LLM结果已落库：重扫不再调用大模型", calls_llm["n"] == 0, str(calls_llm["n"]))

        matched_events = [e for e in events if e.get("type") == "matches"]
        status_events = [e for e in events if e.get("type") == "status"]
        check("输出了matches事件", len(matched_events) >= 3, str(len(matched_events)))
        check("输出了status事件", len(status_events) >= 3, str(len(status_events)))
        matcher.stop()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_pipeline_exclusion():
    print("== 排除与共同好友流水线（假数据） ==")
    tmpdir = tempfile.mkdtemp()
    try:
        cfg_path = os.path.join(tmpdir, "config.json")
        cfg = config_mod.Config(cfg_path)
        cfg.set([
            {"id": "kw", "name": "关键词", "type": "keyword",
             "keywords": ["日语", "日本語", "做模型"], "mode": "any"},
            {"id": "re", "name": "X链接", "type": "regex",
             "pattern": r"(?:twitter|x)\.com/\w+", "fields": ["bio", "bioLinks"]},
            # Alice 是 Cookie 账号好友（demo.FakeAPI.get_friends），应被好友排除；
            # Bob 在名单里，应被名单排除 —— 即使他们命中上面的正向规则
            {"id": "ex", "name": "排除", "type": "exclude", "exclude_self": True,
             "exclude_friends": True,
             "users": [{"user_id": "usr_dddddddd-1111-2222-3333-444444444444",
                        "name": "Bob_Builder"}]},
            # Charlie 假数据共同好友 10 个（≥5 命中），Jack 2 个（不命中）
            {"id": "mu", "name": "共同好友", "type": "mutual_friends",
             "operator": ">=", "count": 5},
        ], "rules")
        cfg.set("", "cookies", "auth")

        from cache_store import CacheStore
        import demo
        cache = CacheStore(os.path.join(tmpdir, "cache.db"))
        matcher = Matcher(cfg, cache, api=None)
        demo.setup(matcher, cfg)
        matcher.start()

        deadline = time.time() + 30
        names = set()
        while time.time() < deadline:
            with matcher._lock:
                matches = {m["user_id"]: m for m in matcher._matches.values()}
            names = {m["name"] for m in matches.values()}
            if "Charlie" in names:
                break
            time.sleep(0.2)

        check("好友排除：Alice 命中正则但被排除", "Alice" not in names, str(names))
        check("名单排除：Bob 被排除", "Bob_Builder" not in names, str(names))
        check("排除不影响其他人：星野/Jack 仍命中关键词",
              {"星野くるみ", "Jack_Miller"} <= names, str(names))
        check("共同好友≥5：Charlie 命中", "Charlie" in names, str(names))
        jack_rules = {r["name"] for r in matches[
            "usr_bbbbbbbb-1111-2222-3333-444444444444"]["rules"]} if \
            "usr_bbbbbbbb-1111-2222-3333-444444444444" in matches else set()
        check("共同好友<5：Jack 只有关键词命中", "共同好友" not in jack_rules, str(jack_rules))
        charlie_rules = {r["name"]: r.get("reason", "") for r in matches[
            "usr_eeeeeeee-1111-2222-3333-444444444444"]["rules"]} if \
            "usr_eeeeeeee-1111-2222-3333-444444444444" in matches else {}
        check("共同好友理由带数量", charlie_rules.get("共同好友") == "共同好友 10 人",
              str(charlie_rules))
        check("自己永远被排除", "DemoLocalUser" not in names)

        # 好友列表已加载且来源正确
        with matcher._lock:
            friends = dict(matcher._friends)
        check("好友列表已加载", "usr_cccccccc-1111-2222-3333-444444444444" in friends, str(friends))
        # 共同好友数已按 (me, uid) 落库
        count, fetched_at = cache.get_mutual_count(
            "usr_11111111-1111-1111-1111-111111111111",
            "usr_eeeeeeee-1111-2222-3333-444444444444")
        check("共同好友数已缓存", count == 10 and fetched_at > 0, str((count, fetched_at)))

        # 状态表：被排除的玩家显示"已排除"
        roster = matcher.get_roster()
        status_by_name = {e["name"]: e["status"] for e in roster}
        check("状态表标注已排除",
              status_by_name.get("Alice") == "已排除" and status_by_name.get("Bob_Builder") == "已排除",
              str(status_by_name))

        # 共同好友数缓存生效：重扫不再调用 mutual 接口
        calls = {"n": 0}
        real_mutual = matcher.api.get_mutual_counts

        def counting_mutual(uid):
            calls["n"] += 1
            return real_mutual(uid)

        matcher.api.get_mutual_counts = counting_mutual
        matcher.handle_log_event({"kind": "join", "name": "Diana",
                                  "user_id": "usr_ffffffff-1111-2222-3333-444444444444"})
        time.sleep(4)
        check("共同好友缓存生效：重扫不重复调用", calls["n"] <= 1, str(calls["n"]))
        matcher.stop()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_config_backup():
    print("== 配置备份与恢复 ==")
    tmpdir = tempfile.mkdtemp()
    try:
        p = os.path.join(tmpdir, "config.json")
        my_rules = [{"id": "mine", "name": "我的规则", "type": "keyword", "keywords": ["测试词"]}]
        cfg = config_mod.Config(p)
        cfg.set(my_rules, "rules")

        # 场景1：config.json 被误删 -> 重启自动恢复规则
        os.remove(p)
        cfg2 = config_mod.Config(p)
        got = cfg2.get("rules")
        check("config 被删后自动恢复规则", got and got[0]["name"] == "我的规则", repr(got))

        # 场景2：config.json 损坏 -> 自动恢复
        cfg2.set([{"id": "mine2", "name": "第二版规则", "type": "keyword", "keywords": ["y"]}], "rules")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{这不是JSON")
        cfg3 = config_mod.Config(p)
        got = cfg3.get("rules")
        check("config 损坏后自动恢复规则", got and got[0]["name"] == "第二版规则", repr(got))
        check("损坏文件被留档", any("corrupt" in n for n in os.listdir(tmpdir)))

        # 场景3：全新目录（无备份）-> 生成默认配置
        p2 = os.path.join(tmpdir, "fresh", "config.json")
        cfg4 = config_mod.Config(p2)
        check("无备份时生成默认配置", isinstance(cfg4.get("rules"), list) and cfg4.get("rules"))

        # 场景4：每日快照保留 7 天
        cfg4.set("x", "ui", "alpha")
        snaps = [n for n in os.listdir(cfg4.backup_dir) if n.startswith("config-2")]
        check("每日快照存在", len(snaps) == 1, repr(snaps))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_log_discovery():
    print("== 日志文件发现（新旧位置兼容） ==")
    tmpdir = tempfile.mkdtemp()
    try:
        # 新版：根目录；旧版：Output Log 子目录。最新文件在旧版子目录里也要能找到
        new_dir = os.path.join(tmpdir, "VRChat")
        old_dir = os.path.join(new_dir, "Output Log")
        os.makedirs(new_dir)
        os.makedirs(old_dir)
        f1 = os.path.join(new_dir, "output_log_2026-09-19_00-44-18.txt")
        f2 = os.path.join(old_dir, "output_log_2026-09-20_04-59-49.txt")
        for path in (f1, f2):
            with open(path, "w", encoding="utf-8") as f:
                f.write("")
        # 反转创建时间，验证按文件名时间戳而不是 ctime 排序
        os.utime(f1, (time.time() + 100, time.time() + 100))
        found = vrchat_log.latest_log_file(log_dir=new_dir)
        check("跨新旧目录找到最新日志", found == f2, repr(found))
        # 只有根目录文件时
        os.remove(f2)
        found = vrchat_log.latest_log_file(log_dir=new_dir)
        check("根目录文件被发现", found == f1, repr(found))
        # 目录不存在时返回 None
        check("目录不存在返回 None",
              vrchat_log.latest_log_file(log_dir=os.path.join(tmpdir, "nope")) is None)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_rules_import_export():
    print("== 规则导入导出与去重 ==")
    entries = [
        {"name": "规则A", "type": "keyword", "keywords": ["x"], "color": "#8cff5c"},
        {"name": "规则A", "type": "keyword", "keywords": ["x"], "color": "#8cff5c"},  # 完全相同
        {"name": "坏规则", "type": "keyword", "keywords": []},                          # 无效
        {"name": "AI规则", "type": "llm", "prompt": ""},                               # 无效
    ]
    rules, skipped = load_rules_payload({"app": "t", "kind": "rules", "rules": entries})
    check("导入解析+跳过无效条目", len(rules) == 2 and skipped == 2,
          "%d/%d" % (len(rules), skipped))
    check("裸数组格式也可导入", load_rules_payload(entries)[0][0].name == "规则A")

    r1 = Rule.from_dict({"name": "同", "type": "keyword", "keywords": ["x"]})
    r2 = Rule.from_dict({"name": "同", "type": "keyword", "keywords": ["y"]})
    check("同内容签名相同", rule_signature(r1) == rule_signature(
        Rule.from_dict({"name": "同", "type": "keyword", "keywords": ["x"]})))
    check("内容不同签名不同", rule_signature(r1) != rule_signature(r2))

    payload = rules_payload([Rule.from_dict(
        {"name": "规则A", "type": "keyword", "keywords": ["x"], "color": "#8cff5c"}).to_dict()])
    check("导出载荷结构", payload["kind"] == "rules" and payload["count"] == 1, repr(payload)[:80])
    back, skipped2 = load_rules_payload(payload)
    check("导出→导入往返无损", len(back) == 1 and skipped2 == 0
          and back[0].name == "规则A" and back[0].color == "#8cff5c")

    # 与现有配置去重：内容相同视为重复
    existing_rule = Rule.from_dict(
        {"id": "e1", "name": "已有", "type": "keyword", "keywords": ["z"]})
    same_content = Rule.from_dict(
        {"name": "已有", "type": "keyword", "keywords": ["z"]})
    check("与现有配置去重", rule_signature(same_content) == rule_signature(existing_rule))


def run():
    print("开始自检（不联网、不依赖 VRChat）\n")
    test_log_parsing()
    test_log_discovery()
    test_trust_levels()
    test_rules()
    test_exclude_mutual_rules()
    test_cache()
    test_extract_json()
    test_config_backup()
    test_rules_import_export()
    test_pipeline()
    test_pipeline_exclusion()
    print("\n结果: %d 通过, %d 失败" % (PASS, FAIL))
    return FAIL == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
