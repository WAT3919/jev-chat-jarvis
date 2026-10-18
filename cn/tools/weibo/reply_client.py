"""Candidate reply drafting via any OpenAI-compatible /chat/completions endpoint.

Prompt templates adapted from app/jev/ReplyClient.kt (weibo comments instead of chat
replies). Parsing mirrors its parseThree(): JSON array first, line-split fallback,
pad to 3 candidates.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from jev_client import JevError, redact_secrets, replies_url

SYSTEM_PROMPT = (
    "你是中文微博回复助手。只输出一个 JSON 数组，含且仅含 3 条候选回复文本，"
    "三条策略要有区别（例如：一条认同承接、一条补充事实或提问、一条简短幽默或质疑）。"
    "每条不超过 40 字，口语、自然、像真人在微博评论区发言。"
    "不要解释，不要加引号以外的内容，直接输出 JSON 数组。"
)

# Used when the judge flags risk: no agree-and-amplify branch for unverified or
# inflammatory content, so candidates never help spread a risky post.
SYSTEM_PROMPT_SKEPTICAL = (
    "你是中文微博回复助手。只输出一个 JSON 数组，含且仅含 3 条候选回复文本，"
    "三条策略要有区别（例如：一条求证质疑、一条补充已知事实或冷静提醒、一条简短幽默）。"
    "每条不超过 40 字，口语、自然、像真人在微博评论区发言。"
    "不要认同或转述帖子里未经证实的内容，不要煽动情绪。"
    "不要解释，不要加引号以外的内容，直接输出 JSON 数组。"
)

PAD_REPLY = "（先看一下再回）"


def build_user_prompt(author: str, text: str, retweeted_text: str = "",
                      context: str = "", perspective: str = "",
                      is_comment: bool = False) -> str:
    lines = []
    if perspective:
        lines += [f"你的身份：{perspective}"]
    if is_comment:
        lines += [f"评论人：{author}", "评论内容：", text]
    else:
        lines += [f"帖子作者：{author}", "帖子内容：", text]
    if retweeted_text:
        lines += ["转发附带内容：", retweeted_text]
    if context:
        lines += ["相关上下文：", context]
    lines.append("请给出 3 条候选回复。")
    return "\n".join(lines)


def _collect_arrays(raw: str) -> list[str]:
    """Collect candidate texts from model output.

    Handles the common shapes chat models actually produce:
      ["a","b","c"]          one array of texts
      ["a"]  ["b"]  ["c"]    several arrays on one line (common model failure)
      [["a"],["b"],["c"]]    nested
      prose with an array inside
    If nothing parses, retries once after stripping JSON trailing commas
    (a model failure mode seen in the wild: ["a","b","c",]).
    """
    texts = _scan_arrays(raw)
    if not texts:
        cleaned = re.sub(r",\s*\]", "]", raw)
        if cleaned != raw:
            texts = _scan_arrays(cleaned)
    return texts


def _scan_arrays(raw: str) -> list[str]:
    texts: list[str] = []
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        start = raw.find("[", idx)
        if start == -1:
            break
        try:
            obj, end = decoder.raw_decode(raw[start:])
        except json.JSONDecodeError:
            idx = start + 1
            continue
        idx = start + max(1, end)
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, str):
                    texts.append(item)
                elif isinstance(item, list):
                    texts.extend(t for t in item if isinstance(t, str))
    return [t.strip() for t in texts if t.strip()]


def parse_three(raw: str) -> list[str]:
    """Parse the model output into exactly 3 candidates, with fallbacks."""
    if not raw:
        return [PAD_REPLY] * 3

    # Array shapes first (see _collect_arrays).
    texts = _collect_arrays(raw)
    if texts:
        return (texts + [PAD_REPLY] * 3)[:3]

    # Fallback: one candidate per line, strip list markers/prefix noise.
    texts = [
        re.sub(r'^[-*\d.\s"\'“”]+', "", line).strip()
        for line in raw.splitlines()
    ]
    texts = [t for t in texts if t]
    if not texts:
        return [PAD_REPLY] * 3
    return (texts + [PAD_REPLY] * 3)[:3]


class ReplyDrafter:
    """Drafts 3 candidate replies via chat completions."""

    def __init__(self, base_url: str, model: str, api_key: str,
                 secrets: list[str] | None = None, timeout: float = 40) -> None:
        self.url = replies_url(base_url)
        self.model = model
        self.key = (api_key or "").strip()
        self.secrets = secrets or []
        self.timeout = timeout
        if not self.key:
            raise JevError("no API key configured for the reply route")

    def chat(self, system: str, user: str, temperature: float = 0.3,
             max_tokens: int = 2500) -> str:
        """One generic chat round on the reply route; returns message content.

        Used for features beyond 3-candidate drafting (e.g. relationship
        summaries). DeepSeek-specific tuning mirrors draft().
        """
        payload = {
            "model": self.model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if "deepseek" in self.url:
            payload["thinking"] = {"type": "disabled"}
            payload["max_tokens"] = max_tokens
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        headers = {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        }
        req = urllib.request.Request(self.url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = redact_secrets(exc.read().decode("utf-8", errors="replace"),
                                        self.secrets)[:400]
            except Exception:
                detail = ""
            raise JevError(f"reply API HTTP {exc.code}: {detail}", exc.code) from None
        except Exception as exc:
            raise JevError(f"reply API request failed: {exc}") from None
        try:
            return resp_body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise JevError("reply API returned no message content") from None

    def draft(self, author: str, text: str, retweeted_text: str = "",
              context: str = "", temperature: float = 0.8,
              skeptical: bool = False, perspective: str = "",
              is_comment: bool = False) -> list[str]:
        payload = {
            "model": self.model,
            "temperature": temperature,
            "messages": [
                {"role": "system",
                 "content": SYSTEM_PROMPT_SKEPTICAL if skeptical else SYSTEM_PROMPT},
                {"role": "user",
                 "content": build_user_prompt(author, text, retweeted_text, context,
                                              perspective, is_comment)},
            ],
        }
        if "deepseek" in self.url:
            # DeepSeek V4 defaults to thinking mode; short replies do not need
            # it, and json_object mode guarantees a parseable JSON array.
            payload["thinking"] = {"type": "disabled"}
            payload["response_format"] = {"type": "json_object"}
            payload["max_tokens"] = 800
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        headers = {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        }
        req = urllib.request.Request(self.url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = redact_secrets(exc.read().decode("utf-8", errors="replace"),
                                        self.secrets)[:400]
            except Exception:
                detail = ""
            raise JevError(f"reply API HTTP {exc.code}: {detail}", exc.code) from None
        except Exception as exc:
            raise JevError(f"reply API request failed: {exc}") from None

        try:
            content = resp_body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise JevError("reply API returned no message content") from None
        return parse_three(content)