"""规则定义与本地匹配（关键词 / 正则）。LLM 规则的匹配在 llm_matcher.py。"""
import hashlib
import json
import re
from dataclasses import dataclass, field

KEYWORD = "keyword"
REGEX = "regex"
LLM = "llm"

ALL_FIELDS = ("bio", "bioLinks", "displayName", "pronouns", "statusDescription", "tags")


def _valid_color(value):
    """只接受 #rrggbb 形式的颜色，其他一律清空（回退自动取色）。"""
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", str(value or "").strip())
    return "#" + m.group(1).lower() if m else ""


@dataclass
class Rule:
    id: str = ""
    name: str = ""
    type: str = KEYWORD
    keywords: list = field(default_factory=list)
    mode: str = "any"          # any=命中任一关键词 / all=全部命中
    pattern: str = ""
    fields: list = field(default_factory=lambda: ["bio"])
    prompt: str = ""           # 仅 llm 类型
    color: str = ""            # 悬浮窗标签颜色（#rrggbb）；空 = 按名字自动取色
    enabled: bool = True

    @classmethod
    def from_dict(cls, data):
        rule = cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or "未命名规则"),
            type=str(data.get("type") or KEYWORD).lower(),
            keywords=[str(k) for k in (data.get("keywords") or [])],
            mode="all" if data.get("mode") == "all" else "any",
            pattern=str(data.get("pattern") or ""),
            fields=[str(f) for f in (data.get("fields") or ["bio"]) if str(f) in ALL_FIELDS],
            prompt=str(data.get("prompt") or ""),
            color=_valid_color(data.get("color")),
            enabled=bool(data.get("enabled", True)),
        )
        if not rule.id:
            rule.id = "rule-" + hashlib.md5(
                json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:8]
        if not rule.fields:
            rule.fields = ["bio"]
        return rule

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "type": self.type,
            "keywords": self.keywords, "mode": self.mode, "pattern": self.pattern,
            "fields": self.fields, "prompt": self.prompt, "color": self.color,
            "enabled": self.enabled,
        }


def rule_hash(rule):
    """规则内容指纹（参与 LLM 缓存 key：改了规则文本就自动失效重算）。"""
    if rule.type == LLM:
        payload = {"type": LLM, "prompt": rule.prompt}
    elif rule.type == KEYWORD:
        payload = {"type": KEYWORD, "keywords": sorted(rule.keywords), "mode": rule.mode,
                   "fields": sorted(rule.fields)}
    else:
        payload = {"type": REGEX, "pattern": rule.pattern, "fields": sorted(rule.fields)}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def fields_text(profile, fields):
    """取出要匹配的文本片段列表。"""
    texts = []
    for name in fields:
        value = profile.get(name)
        if name == "bioLinks":
            texts.extend(str(v) for v in (value or []))
        elif isinstance(value, list):
            texts.extend(str(v) for v in value)
        elif value:
            texts.append(str(value))
    return texts


def match_keyword(rule, profile):
    """返回命中的关键词列表（保留关键词原样大小写用于展示）；不符合则空列表。

    匹配用 casefold()，保证不区分大小写（比 lower() 覆盖更全的 Unicode 大小写折叠）。
    """
    texts = [t.casefold() for t in fields_text(profile, rule.fields)]
    kws = [(k.strip(), k.strip().casefold()) for k in rule.keywords if k.strip()]
    if not kws:
        return []
    if rule.mode == "all":
        hits = [orig for orig, folded in kws if any(folded in t for t in texts)]
        return hits if len(hits) == len(kws) else []
    return [orig for orig, folded in kws if any(folded in t for t in texts)]


def match_regex(rule, profile):
    """返回命中的文本片段列表（去重）；不符合则空列表。"""
    if not rule.pattern:
        return []
    try:
        compiled = re.compile(rule.pattern, re.IGNORECASE)
    except re.error:
        return []
    hits = []
    for text in fields_text(profile, rule.fields):
        m = compiled.search(text)
        if m and m.group(0) not in hits:
            hits.append(m.group(0))
    return hits


def match_local(rule, profile):
    """本地规则统一入口，返回命中描述列表。"""
    if rule.type == KEYWORD:
        return match_keyword(rule, profile)
    if rule.type == REGEX:
        return match_regex(rule, profile)
    return []


def rule_signature(rule):
    """规则内容指纹（不含 id 与启用状态）：内容完全相同才视为重复，导入去重用。"""
    return json.dumps({
        "name": rule.name, "type": rule.type,
        "keywords": sorted(rule.keywords), "mode": rule.mode,
        "pattern": rule.pattern, "fields": sorted(rule.fields),
        "prompt": rule.prompt, "color": rule.color,
    }, ensure_ascii=False, sort_keys=True)


def rules_payload(rules_dicts):
    """打包成导出文件载荷。"""
    return {"app": "vrchat-profile-check", "kind": "rules", "version": 1,
            "count": len(rules_dicts), "rules": rules_dicts}


def load_rules_payload(data):
    """解析导入文件内容（接受导出载荷或裸数组）。

    返回 (Rule 列表, 无效/不完整条数)。缺必需内容（关键词/正则/判断要求为空）的算无效。
    """
    entries = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return [], 0
    rules, skipped = [], 0
    for entry in entries:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        rule = Rule.from_dict(entry)
        if rule.type not in (KEYWORD, REGEX, LLM) or not rule.name:
            skipped += 1
            continue
        if ((rule.type == KEYWORD and not rule.keywords)
                or (rule.type == REGEX and not rule.pattern)
                or (rule.type == LLM and not rule.prompt)):
            skipped += 1
            continue
        rules.append(rule)
    return rules, skipped
