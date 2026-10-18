"""Render the analysis result as a readable panel (or raw JSON for --json)."""

from __future__ import annotations

import json
import textwrap

# English answer keys -> Chinese display labels, per question.
CHOICE_LABELS = {
    "post_intent": {
        "share_opinion": "陈述观点",
        "express_emotion": "表达情绪",
        "promote_ad": "带货/引流",
        "drum_up_support": "带节奏拱火",
        "spread_unverified": "传播未证实消息",
        "seek_help": "求助",
        "troll_attack": "引战挑衅",
        "casual_share": "日常分享",
    },
    "credibility": {
        "verified_fact": "有据可查",
        "plausible_unverified": "貌似可信但无来源",
        "misleading_exaggeration": "夸大/断章取义",
        "clear_falsehood": "明显不实",
        "opinion_not_factual": "纯观点(无事实断言)",
    },
    "best_action": {
        "agree_support": "认同支持",
        "politely_question": "礼貌提问",
        "fact_check_refute": "质疑核查",
        "correct_with_source": "有据纠正",
        "ignore": "不理会",
        "report": "举报/拉黑",
    },
    "poster_needs": {
        "attention": "求关注",
        "dissemination": "求扩散",
        "agreement": "求认同",
        "help": "求帮助",
        "engagement_traffic": "要热度流量",
        "nothing": "无诉求",
    },
    "best_reply": {},
}

QUESTION_TITLES = {
    "post_intent": "发布者意图",
    "danger_level": "内容风险",
    "credibility": "事实可信度",
    "aggression": "攻击性",
    "should_engage": "该不该回",
    "best_action": "最佳动作",
    "poster_needs": "对方要什么",
}

# Short Chinese bin labels for the two score questions (full English bins stay
# in the payload / --json output).
SCORE_BIN_LABELS = {
    "danger_level": [
        "无害日常分享", "温和观点/情绪", "公共话题评论", "争议性单边叙事",
        "无来源未证实消息", "点名未证实指控", "明显诽谤/人肉暗示",
        "诈骗/仇恨/人肉", "诈骗教程/威胁", "明显违法内容",
    ],
    "aggression": [
        "平和", "礼貌异议", "热烈但不对人", "对事嘲讽", "对人嘲讽",
        "点名辱骂", "群体攻击", "号召围攻", "泄露隐私/人肉", "暴力威胁",
    ],
}

_WIDTH = 64


def _pct(value: float | None, decimals: int = 0) -> str:
    if value is None:
        return ""
    return f"{value * 100:.{decimals}f}%".replace(".0%", "%")


def _legend_text(question_key: str, index: int) -> str:
    """Short Chinese bin label; falls back to our own English criteria text."""
    labels = SCORE_BIN_LABELS.get(question_key)
    if labels and 0 <= index < len(labels):
        return labels[index]
    from questions import WEIBO_QUESTIONS
    criteria = WEIBO_QUESTIONS[question_key]["criteria"]
    if isinstance(criteria, list) and 0 <= index < len(criteria):
        return criteria[index]
    return ""


def _answer_to_text(question_key: str, answer: dict) -> str:
    """answers.{key} -> display string. Only render what exists."""
    if not answer:
        return "（无结果）"
    qtype = _question_type(question_key)
    if qtype == "noul":
        value = _as_float(answer.get("noul"))
        if value is None:
            return "（无结果）"
        return f"{'是' if value >= 0.5 else '否'} ({_pct(value)})"
    if qtype == "choice":
        choice = answer.get("choice")
        confidence = _as_float(answer.get("confidence"))
        label = CHOICE_LABELS.get(question_key, {}).get(choice, choice or "?")
        return f"{label} [{_pct(confidence)}]"
    if qtype == "score":
        score, max_level = _score_index(question_key, answer)
        if score is None:
            return "（无结果）"
        legend = _legend_text(question_key, score)
        return f"{score}/{max_level}  {legend}"
    return json.dumps(answer, ensure_ascii=False)


def _question_type(question_key: str) -> str:
    from questions import WEIBO_QUESTIONS
    meta = WEIBO_QUESTIONS.get(question_key)
    return meta["type"] if meta else "choice"


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _score_index(question_key: str, answer: dict) -> tuple[int | None, int]:
    """Preferred bin: probabilities-weighted argmax, fallback to the score field."""
    from questions import WEIBO_QUESTIONS
    max_level = len(WEIBO_QUESTIONS[question_key]["criteria"]) - 1
    probs = answer.get("probabilities")
    if isinstance(probs, dict) and probs:
        weighted = sum(float(i) * _as_float(p) for i, p in probs.items())
        return max(0, min(max_level, int(round(weighted)))), max_level
    score = _as_float(answer.get("score"))
    if score is None:
        return None, max_level
    return max(0, min(max_level, int(round(score)))), max_level


def _wrap(text: str, width: int, indent: str) -> str:
    if not text:
        return ""
    lines = textwrap.wrap(text, width=width, replace_whitespace=False,
                          break_long_words=False, break_on_hyphens=False)
    return "\n".join(f"{indent}{line}" for line in lines)


def render(result: dict) -> str:
    """Plain-text panel. ASCII delimiters so it survives any console codepage."""
    out: list[str] = []
    line = "=" * _WIDTH
    thin = "-" * _WIDTH
    post = result["post"]
    judge = result["judge"]
    replies = result["replies"]

    out.append(line)
    out.append(" 微博帖子分析")
    out.append(thin)
    author = post.get("author") or "?"
    out.append(f" 作者 : {author}")
    source = post.get("source") or "手动输入"
    out.append(f" 来源 : {source}")
    out.append(thin)
    out.append(f" {('评论' if post.get('kind') == 'comment' else '帖子')} :")
    out.append(_wrap(post["text"], _WIDTH - 6, "   ") or "   （空）")
    if post.get("retweeted_text"):
        out.append(" 转发附带 :")
        out.append(_wrap(post["retweeted_text"], _WIDTH - 6, "   "))
    if post.get("context"):
        out.append(" 上下文 :")
        out.append(_wrap(post["context"], _WIDTH - 6, "   "))
    out.append(thin)

    answers = judge.get("answers") or {}
    backend = judge.get("backend")
    if backend and backend != "jev":
        out.append(f" 判断 ({judge['model']} · 聊天模型判断 · {judge['latency_ms']}ms)")
    else:
        out.append(f" 判断 ({judge['model']} · {judge['latency_ms']}ms)")
    for qkey, title in QUESTION_TITLES.items():
        if qkey in answers:
            out.append(f"   {title} : {_answer_to_text(qkey, answers[qkey])}")

    if replies.get("ranked"):
        out.append(thin)
        out.append(" 候选回复 (按最合适排序)")
        for i, item in enumerate(replies["ranked"], 1):
            prob = _pct(item["probability"], decimals=0)
            out.append(f"   {i}. [{prob}] {item['text']}")
    elif replies.get("candidates"):
        out.append(thin)
        out.append(" 候选回复 (草拟顺序，排序失败)")
        for i, text in enumerate(replies["candidates"], 1):
            out.append(f"   {i}. {text}")
    if replies.get("draft_error"):
        out.append(f"   （草拟失败: {replies['draft_error']}）")
    elif replies.get("rank_error"):
        out.append(f"   （排序失败: {replies['rank_error']}）")

    cost = result.get("cost")
    tokens_in = result.get("tokens_in") or 0
    tokens_out = result.get("tokens_out") or 0
    if cost is not None or tokens_in or tokens_out:
        out.append(thin)
        parts: list[str] = []
        if cost is not None:
            parts.append(f"费用 : ${cost:.5f}")
        if tokens_in or tokens_out:
            parts.append(f"token : 入 {tokens_in} / 出 {tokens_out}")
        parts.append(f"总耗时 {result['total_latency_ms']}ms")
        out.append(" " + " · ".join(parts))
    out.append(line)
    return "\n".join(out)


def render_json(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)