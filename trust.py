"""VRChat 信任等级：映射与配色取自 VRCX (src/shared/utils/userTransforms.js +
src/stores/settings/appearance.js TRUST_COLOR_DEFAULTS)。

注意：system_trust_veteran 标签已被 VRChat 淘汰，存量账号显示为 Trusted User。
"""


def user_trust(tags, developer_type=""):
    """从用户 tags 返回 (等级名, 颜色, 排序值)。"""
    tags = tags or []
    if not isinstance(tags, (list, tuple)):
        tags = []
    is_moderator = bool(developer_type) and developer_type != "none"
    if "admin_moderator" in tags:
        is_moderator = True
    is_troll = "system_troll" in tags or "system_probable_troll" in tags

    if "system_trust_veteran" in tags:
        name, color_key, sort = "Trusted User", "veteran", 5
    elif "system_trust_trusted" in tags:
        name, color_key, sort = "Known User", "trusted", 4
    elif "system_trust_known" in tags:
        name, color_key, sort = "User", "known", 3
    elif "system_trust_basic" in tags:
        name, color_key, sort = "New User", "basic", 2
    else:
        name, color_key, sort = "Visitor", "untrusted", 1

    if is_troll:
        color_key = "troll"
    elif is_moderator:
        color_key = "vip"

    color = TRUST_COLORS.get(color_key, TRUST_COLORS["untrusted"])
    return name, color, sort


TRUST_COLORS = {
    "untrusted": "#CCCCCC",
    "basic": "#1778FF",
    "known": "#2BCF5C",
    "trusted": "#FF7B42",
    "veteran": "#B18FFF",
    "vip": "#FF2626",
    "troll": "#782F2F",
}
