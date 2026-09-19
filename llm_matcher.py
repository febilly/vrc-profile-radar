"""LLM 规则匹配：分块批量请求 + 按用户缓存。

设计取舍（批量 vs 单人并发）：
  - 单人并发：每个用户一次请求，规则文本重复 N 次，token 开销大；
  - 全拼一块：整间房 30+ 人时上下文过长、准确率下降，且一个用户的 bio 变动就作废整个结果；
  - 这里采用折中：按 batch_size（默认 10 人）分块批量请求，整间房只发几次请求；
    缓存按 (规则内容 + 用户id + bio 内容) 哈希，bio 不变的用户永远不会重复计费；
    新人进房只把没缓存的人发出去。某一块解析失败时自动降级为该块内逐人请求。
兼容任何 OpenAI Chat Completions 风格端点（OpenAI / DeepSeek / OpenRouter / 本地 Ollama 等）。
"""
import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import rules as rules_mod


class LLMNotConfigured(Exception):
    pass


def extract_json(text):
    """从模型输出中抠出第一个 JSON 对象（容忍 ```json 围栏和前后闲话）。"""
    text = re.sub(r"```(?:json)?", "", text or "")
    start = text.find("{")
    while start >= 0:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[start:])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        start = text.find("{", start + 1)
    return None


class LLMMatcher:
    def __init__(self, config, cache):
        self.config = config
        self.cache = cache
        self.result_cache_max = int(config.get("llm", "result_cache_max", default=10000) or 10000)
        self._limit_lock = threading.Semaphore(max(1, int(config.get("llm", "concurrency", default=2) or 2)))

    @property
    def configured(self):
        return bool(self.config.get("llm", "api_key", default=""))

    def _cache_key(self, rule, user):
        bio = json.dumps(
            [user.get("displayName"), user.get("bio"), user.get("bioLinks")],
            ensure_ascii=False,
        )
        digest = hashlib.sha256(bio.encode("utf-8")).hexdigest()[:16]
        return "%s|%s|%s" % (rules_mod.rule_hash(rule), user.get("user_id", ""), digest)

    def match_rule(self, rule, users, progress=None):
        """对一组用户执行一条 LLM 规则。

        users: [{user_id, displayName, bio, bioLinks}]
        返回 {user_id: {"match": bool, "reason": str}}；LLM 未配置时抛 LLMNotConfigured。
        结果会写入 LLM 结果缓存（按数量上限 LRU 淘汰）。
        """
        if not self.configured:
            raise LLMNotConfigured("LLM API Key 未配置")

        results = {}
        pending = []
        pending_keys = {}
        for user in users:
            key = self._cache_key(rule, user)
            cached = self.cache.get_llm(key)
            if cached is not None:
                results[user["user_id"]] = cached
            else:
                pending.append(user)
                pending_keys[user["user_id"]] = key

        if not pending:
            return results

        batch_size = max(1, int(self.config.get("llm", "batch_size", default=10) or 10))
        chunks = [pending[i : i + batch_size] for i in range(0, len(pending), batch_size)]
        total_done = 0

        def work(chunk):
            with self._limit_lock:
                return self._match_chunk_with_fallback(rule, chunk)

        concurrency = max(1, int(self.config.get("llm", "concurrency", default=2) or 2))
        with ThreadPoolExecutor(max_workers=min(len(chunks), concurrency)) as pool:
            futures = {pool.submit(work, chunk): chunk for chunk in chunks}
            for future in as_completed(futures):
                chunk_results = future.result()
                for user_id, res in chunk_results.items():
                    results[user_id] = res
                    key = pending_keys.get(user_id)
                    if key:
                        # 结果落库：同一段简介 + 同一条规则下次不再花钱重调
                        self.cache.put_llm(key, res, max_count=self.result_cache_max)
                total_done += len(chunk_results)
                if progress:
                    progress(total_done, len(pending))

        return results

    def _match_chunk_with_fallback(self, rule, chunk):
        try:
            return self._match_chunk(rule, chunk)
        except Exception:
            results = {}
            for user in chunk:
                try:
                    results.update(self._match_chunk(rule, [user]))
                except Exception:
                    results[user["user_id"]] = {"match": False, "reason": "LLM请求失败"}
            return results

    def _match_chunk(self, rule, chunk):
        base_url = (self.config.get("llm", "base_url", default="") or "").strip().rstrip("/")
        model = (self.config.get("llm", "model", default="gpt-4o-mini") or "").strip()
        timeout = int(self.config.get("llm", "timeout_seconds", default=90) or 90)
        api_key = (self.config.get("llm", "api_key", default="") or "").strip()

        user_list = [
            {
                "id": u.get("user_id", ""),
                "name": u.get("displayName", ""),
                "bio": u.get("bio", ""),
                "links": u.get("bioLinks") or [],
            }
            for u in chunk
        ]
        system = (
            "你是一个规则匹配引擎。根据给定的规则，逐个判断每个 VRChat 用户的资料是否符合。"
            '只输出一个 JSON 对象，格式：{"results": [{"id": "<用户id>", "match": true或false, '
            '"reason": "<20字以内的简短理由>"}]}。'
            "用户列表中的每一个人都必须有一条对应结果，不得遗漏或增减。reason 用简体中文。"
        )
        user_content = "## 匹配规则\n%s\n\n## 用户列表\n%s" % (
            rule.prompt,
            json.dumps(user_list, ensure_ascii=False),
        )

        resp = requests.post(
            base_url + "/chat/completions",
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
            json={
                "model": model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_content},
                ],
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError("LLM HTTP %s: %s" % (resp.status_code, resp.text[:200]))
        content = resp.json()["choices"][0]["message"]["content"]

        parsed = extract_json(content)
        if not parsed or not isinstance(parsed.get("results"), list):
            raise ValueError("无法解析 LLM 输出")

        results = {}
        by_id = {}
        for item in parsed["results"]:
            if isinstance(item, dict) and "id" in item:
                by_id[str(item["id"])] = item
        for user in chunk:
            uid = user["user_id"]
            item = by_id.get(uid)
            if item is None:
                results[uid] = {"match": False, "reason": "LLM未返回该用户"}
            else:
                results[uid] = {
                    "match": bool(item.get("match")),
                    "reason": str(item.get("reason", ""))[:120],
                }
        return results
