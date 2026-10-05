"""演示模式：不连 VRChat、不联网，用假日志事件+假资料走完整流水线，用于体验和自测。"""
import threading
import time

from llm_matcher import LLMMatcher

LOCAL_UID = "usr_11111111-1111-1111-1111-111111111111"

CANNED_PROFILES = [
    {
        "id": "usr_aaaaaaaa-1111-2222-3333-444444444444",
        "displayName": "星野くるみ",
        "bio": "VRChatで日本語を勉強中です！写真取るのが好きです📷 よろしくね",
        "bioLinks": [],
        "pronouns": "she/her",
        "tags": ["system_trust_basic"],
        "currentAvatarThumbnailImageUrl": "",
    },
    {
        "id": "usr_bbbbbbbb-1111-2222-3333-444444444444",
        "displayName": "Jack_Miller",
        "bio": "Just chilling in VRChat. I can speak a little Japanese. 日语水平一般，欢迎来聊天",
        "bioLinks": ["https://jack-miller.example.com"],
        "pronouns": "he/him",
        "tags": ["system_trust_known"],
        "currentAvatarThumbnailImageUrl": "",
    },
    {
        "id": "usr_cccccccc-1111-2222-3333-444444444444",
        "displayName": "Alice",
        "bio": "hi! follow me on twitter https://x.com/alice_vrc 迷路しやすい",
        "bioLinks": ["https://x.com/alice_vrc", "https://alice.example.com"],
        "pronouns": "",
        "tags": [],
        "currentAvatarThumbnailImageUrl": "",
    },
    {
        "id": "usr_dddddddd-1111-2222-3333-444444444444",
        "displayName": "Bob_Builder",
        "bio": "全职做模型的，Blender/Unity 都会，接过几次 commissions。也写点小工具，算半个开发者。平时喜欢逛世界。",
        "bioLinks": ["https://bobbooth.example.com"],
        "pronouns": "",
        "tags": ["system_trust_veteran"],
        "currentAvatarThumbnailImageUrl": "",
    },
    {
        "id": "usr_eeeeeeee-1111-2222-3333-444444444444",
        "displayName": "Charlie",
        "bio": "普通玩家，下班上来瞎逛，偶尔蹦迪",
        "bioLinks": [],
        "pronouns": "",
        "tags": [],
        "currentAvatarThumbnailImageUrl": "",
    },
    {
        "id": "usr_ffffffff-1111-2222-3333-444444444444",
        "displayName": "Diana",
        "bio": "VRChat 摄影 / 视频剪辑，出片请私信。世界打卡爱好者。",
        "bioLinks": ["https://youtube.com/@diana_vrc"],
        "pronouns": "she/her",
        "tags": [],
        "currentAvatarThumbnailImageUrl": "",
    },
    {
        "id": "usr_77777777-1111-2222-3333-444444444444",
        "displayName": "Eve",
        "bio": "",
        "bioLinks": [],
        "pronouns": "",
        "tags": [],
        "currentAvatarThumbnailImageUrl": "",
    },
]

EXTRA_PLUGIN_PROFILES = [
    {
        "id": "usr_88888888-1111-2222-3333-444444444444",
        "displayName": "PluginDemoUser",
        "bio": "插件演示用户，包含示例外部链接",
        "bioLinks": ["https://example.com/?r=demo_code_12345"],
        "pronouns": "",
        "tags": [],
        "currentAvatarThumbnailImageUrl": "",
    },
]


class FakeAPI:
    """代替 VRChatAPI：内存里返回假资料，模拟少量延迟。"""

    configured = True

    def __init__(self, latency=0.15, extra_profiles=None):
        self.latency = latency
        all_profiles = list(CANNED_PROFILES) + list(extra_profiles or [])
        self.by_id = {p["id"]: p for p in all_profiles}

    def get_me(self):
        return {"displayName": "DemoLocalUser", "id": LOCAL_UID}

    def get_friends(self):
        """假好友列表：Alice 是 Cookie 账号（演示里即本机账号）的好友。"""
        return [{"id": "usr_cccccccc-1111-2222-3333-444444444444", "displayName": "Alice"}]

    def get_mutual_counts(self, user_id):
        """假共同好友计数（响应形状与真实 API 一致）：Charlie 10 个，其他人 2 个。"""
        return {"friends": 10 if user_id == "usr_eeeeeeee-1111-2222-3333-444444444444" else 2,
                "groups": 0}

    def get_mutual_friends(self, user_id):
        """假共同好友列表：Charlie 有 Alice 和 Bob 作为共同好友，其他人只有 Alice。"""
        alice = {"id": "usr_cccccccc-1111-2222-3333-444444444444", "displayName": "Alice"}
        bob = {"id": "usr_dddddddd-1111-2222-3333-444444444444", "displayName": "Bob"}
        if user_id == "usr_eeeeeeee-1111-2222-3333-444444444444":
            return [alice, bob]
        return [alice]

    def get_profile(self, user_id):
        time.sleep(self.latency)
        profile = self.by_id.get(user_id)
        if profile is None:
            from vrchat_api import VRChatAPIError
            raise VRChatAPIError("用户不存在(404)", status=404)
        return dict(profile)


class FakeLLM(LLMMatcher):
    """只替换网络调用部分，批量/缓存逻辑与真实 LLMMatcher 完全一致。"""

    @property
    def configured(self):
        return True

    def _match_chunk_with_fallback(self, rule, chunk):
        time.sleep(0.6)
        results = {}
        for user in chunk:
            bio = user.get("bio") or ""
            hit = any(w in bio for w in ("做模型", "开发者", "摄影", "视频"))
            results[user["user_id"]] = {
                "match": hit,
                "reason": "演示：资料显示是创作者" if hit else "演示：看起来是普通玩家",
            }
        return results


def setup(matcher, config, console=False, include_plugin_demo=False):
    """把 matcher 换成假数据源并开始播事件脚本。"""
    extra = EXTRA_PLUGIN_PROFILES if include_plugin_demo else None
    matcher.api = FakeAPI(extra_profiles=extra)
    matcher.llm = FakeLLM(config, matcher.cache)
    matcher.debounce = 1.0
    matcher._local_uid = LOCAL_UID

    profiles_to_play = list(CANNED_PROFILES)
    if include_plugin_demo:
        profiles_to_play.extend(EXTRA_PLUGIN_PROFILES)

    def script():
        time.sleep(0.3)
        matcher.handle_log_event({
            "kind": "world", "location": "wrld_4432ea9b-729c-46e3-8eaf-846aa0a37fdd:67646~region(jp)",
            "world_id": "wrld_4432ea9b-729c-46e3-8eaf-846aa0a37fdd", "time": "demo",
        })
        matcher.handle_log_event({"kind": "world_name", "name": "The Great Pug (演示)", "time": "demo"})
        matcher.handle_log_event({"kind": "local", "name": "DemoLocalUser", "user_id": LOCAL_UID, "time": "demo"})
        for profile in profiles_to_play:
            if profile["id"] == LOCAL_UID:
                continue
            time.sleep(0.4)
            matcher.handle_log_event({
                "kind": "join", "name": profile["displayName"],
                "user_id": profile["id"], "time": "demo",
            })
        time.sleep(9)
        matcher.handle_log_event({
            "kind": "join", "name": "Diana",
            "user_id": "usr_ffffffff-1111-2222-3333-444444444444", "time": "demo",
        })
        time.sleep(6)
        matcher.handle_log_event({
            "kind": "leave", "name": "Eve",
            "user_id": "usr_77777777-1111-2222-3333-444444444444", "time": "demo",
        })

    threading.Thread(target=script, daemon=True, name="DemoScript").start()
    if console:
        print("演示模式：脚本将模拟 进房 → %d人加入 → 1人再次加入 → 1人离开" % len(profiles_to_play))
