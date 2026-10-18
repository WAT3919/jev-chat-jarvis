"""Chat-model judge fallback: answer the Jev question set via any OpenAI-compatible
chat/completions endpoint (e.g. DeepSeek official), without the Jev decisions model.

Jev's decisions protocol is the preferred backend (JevJudge in jev_client.py); this
fallback lets the tool run end-to-end on a plain chat key. It asks all questions in
one call and coerces the output into the same "answers" envelope Jev returns:
  noul   -> {"noul": 0..1}
  choice -> {"choice", "confidence", "probabilities"}
  score  -> {"score", "confidence", "probabilities"?, "legend": null placeholder}
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from jev_client import JevError, redact_secrets
from reply_client import replies_url

SYSTEM_PROMPT = (
    "You are a weibo post analysis judge. Given a post and a set of questions, "
    "answer every question as a JSON object keyed by the question key. "
    "Output only that JSON object, nothing else.\n"
    "Answer format per question type:\n"
    "- noul questions: {\"noul\": <number between 0 and 1>}\n"
    "- choice questions: {\"choice\": \"<one of the criteria keys>\", "
    "\"confidence\": <number between 0 and 1>}\n"
    "- score questions: {\"score\": <integer index into the ordered criteria array>, "
    "\"confidence\": <number between 0 and 1>}\n"
    "For choice questions, the choice value MUST be exactly one of the keys in the "
    "question's criteria object. For score questions, score MUST be an integer from "
    "0 to len(criteria)-1. Consider tone and context over surface wording. "
    "If the post is only discussed by nickname or group label rather than a named "
    "private person, do not overrate naming-related bins."
)


def build_user_prompt(state: dict, questions: dict) -> str:
    post = state.get("post") or {}
    lines = ["Weibo post:", ""]
    author = post.get("author")
    if author:
        lines.append(f"Author: {author}")
    lines.append("Text:")
    lines.append(post.get("text", ""))
    if post.get("retweeted_text"):
        lines.append("Retweeted content:")
        lines.append(post["retweeted_text"])
    if post.get("context"):
        lines.append("Context:")
        lines.append(post["context"])
    lines.append("")
    lines.append("Questions (JSON):")
    lines.append(json.dumps(questions, ensure_ascii=False, indent=1))
    lines.append("")
    lines.append("Answer as a JSON object keyed by exactly these question keys.")
    return "\n".join(lines)


def _extract_json_object(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise JevError("chat judge returned no JSON object")
    raw = text[start:end + 1]
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JevError(f"chat judge returned invalid JSON: {exc}") from None
    if not isinstance(obj, dict):
        raise JevError("chat judge returned non-object JSON")
    return obj


def _synthesize_probabilities(keys: list[str], choice: str, conf: float,
                              provided: dict) -> dict:
    """Build a probabilities map summing to 1: chosen gets conf, the rest split
    evenly over the remainder, honoring any valid probabilities the model gave."""
    cleaned = {k: v for k, v in provided.items() if k in keys}
    remaining = [k for k in keys if k != choice and k not in cleaned]
    total = sum(cleaned.values())
    if remaining:
        each = max(0.0, (1.0 - total - conf) / len(remaining))
    else:
        each = 0.0
        conf = 1.0 - total
    cleaned[choice] = conf
    for key in remaining:
        cleaned[key] = each
    total = sum(cleaned.values())
    if total > 0:
        cleaned = {k: max(0.0, v / total) for k, v in cleaned.items()}
    return cleaned


def _coerce_answer(qtype: str, criteria, raw) -> dict:
    """Coerce a model answer into the Jev answer shape; raise JevError if unusable."""
    if not isinstance(raw, dict):
        raise JevError(f"chat judge answer for a {qtype} question is not an object")

    if qtype == "noul":
        try:
            value = float(raw.get("noul"))
        except (TypeError, ValueError):
            raise JevError("chat judge noul answer missing a numeric 'noul' value")
        return {"noul": max(0.0, min(1.0, value)),
                "confidence": value if 0 <= value <= 1 else 0.5}

    if qtype == "score":
        keys = criteria if isinstance(criteria, list) else []
        max_index = max(0, len(keys) - 1)
        try:
            score = int(round(float(raw.get("score"))))
        except (TypeError, ValueError):
            raise JevError("chat judge score answer missing a numeric 'score' value")
        score = max(0, min(max_index, score))
        conf = raw.get("confidence")
        try:
            conf = max(0.0, min(1.0, float(conf)))
        except (TypeError, ValueError):
            conf = 0.5
        return {"score": score, "confidence": conf}

    if qtype == "choice":
        keys = list(criteria) if isinstance(criteria, dict) else []
        if not keys:
            raise JevError("chat judge called on a choice question without criteria keys")
        choice = raw.get("choice")
        probs = raw.get("probabilities")
        if not isinstance(probs, dict):
            probs = {}
        if choice not in keys:
            # tolerate a model that echoed the criteria text instead of the key
            if choice in criteria.values():
                choice = next(k for k, v in criteria.items() if v == choice)
        if choice not in keys:
            raise JevError(f"chat judge choice {choice!r} is not one of {keys}")
        conf = probs.get(choice)
        try:
            conf = max(0.0, min(1.0, float(conf)))
        except (TypeError, ValueError):
            conf = raw.get("confidence")
        try:
            conf = max(0.0, min(1.0, float(conf)))
        except (TypeError, ValueError):
            conf = 0.5
        provided = {}
        for k, v in probs.items():
            if k in keys and isinstance(v, (int, float)) and v >= 0:
                provided[k] = float(v)
        cleaned = _synthesize_probabilities(keys, choice, conf, provided)
        return {"choice": choice, "confidence": conf, "probabilities": cleaned}


class ChatJudge:
    """Answers the Jev question set through chat/completions."""

    def __init__(self, base_url: str, model: str, api_key: str,
                 secrets: list[str] | None = None, timeout: float = 60) -> None:
        self.url = replies_url(base_url)
        self.model = model
        self.key = (api_key or "").strip()
        self.secrets = secrets or []
        self.timeout = timeout
        if not self.key:
            raise JevError("no API key configured for the judge route")

    def _post(self, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        }
        req = urllib.request.Request(self.url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = redact_secrets(exc.read().decode("utf-8", errors="replace"),
                                        self.secrets)[:400]
            except Exception:
                detail = ""
            raise JevError(f"judge chat API HTTP {exc.code}: {detail}", exc.code) from None
        except Exception as exc:
            raise JevError(f"judge chat API request failed: {exc}") from None

    def ask(self, state: dict, questions: dict) -> dict:
        """One chat round answering all questions. Returns Jev-shaped response.

        On a bad JSON answer, retries once; second failure is reported as JevError.
        """
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(state, questions)},
            ],
        }
        if "deepseek" in self.url:
            # DeepSeek V4 defaults to thinking mode (reasoning_effort=high);
            # disable it for short structured answers, and force valid JSON.
            payload["thinking"] = {"type": "disabled"}
            payload["response_format"] = {"type": "json_object"}
            payload["max_tokens"] = 3000
        body = self._post(payload)
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise JevError("judge chat API returned no message content") from None

        try:
            raw_answers = _extract_json_object(content)
        except JevError:
            body = self._post(payload)  # one retry on malformed output
            try:
                content = body["choices"][0]["message"]["content"]
                raw_answers = _extract_json_object(content)
            except (KeyError, IndexError, TypeError, JevError) as exc:
                raise JevError(f"chat judge output unusable even after retry: {exc}") from None

        answers: dict = {}
        for qkey, qmeta in questions.items():
            if qkey not in raw_answers:
                raise JevError(f"chat judge did not answer question {qkey}")
            answers[qkey] = _coerce_answer(
                qmeta.get("type", "choice"),
                qmeta.get("criteria"),
                raw_answers[qkey],
            )

        usage = body.get("usage") or {}
        tokens_in = usage.get("prompt_tokens") or usage.get("input_tokens")
        tokens_out = usage.get("completion_tokens") or usage.get("output_tokens")
        out_usage: dict = {}
        if tokens_in is not None:
            out_usage["input_tokens"] = tokens_in
        if tokens_out is not None:
            out_usage["output_tokens"] = tokens_out
        return {"answers": answers, "usage": out_usage}