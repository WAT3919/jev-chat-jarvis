#!/usr/bin/env python3
"""Weibo post analyzer — local CLI tool.

Input a weibo post (pasted text, text file, or a post URL that gets fetched with
your cookie), and it:
  1. judges the post with the Jev decisions model (7 dimensions),
  2. drafts 3 candidate replies via an OpenAI-compatible endpoint,
  3. ranks the candidates with Jev,
  4. prints a readable panel (or --json for scripts).

Config lives in tools/weibo/config.json (see config.example.json); the OpenRouter
key can also come from the OPENROUTER_API_KEY env var, the weibo cookie from
WEIBO_COOKIE. Keys and cookies are never logged or written.

Usage:
  python analyze.py --text "帖子正文..." [--author 昵称] [--context ...]
  python analyze.py --file posts.txt [--author 昵称]
  python analyze.py --url https://weibo.com/5979979070/AbCdEf123456
  python analyze.py --url <url> --dump          # fetch only, print raw JSON
  python analyze.py --text "..." --payload      # print state+questions, no API call
  python analyze.py                             # interactive prompt
  python analyze.py ... --json                  # machine-readable full result
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from questions import WEIBO_QUESTIONS, build_rank_question, build_state
from jev_client import JevError, JevJudge, redact_secrets
from reply_client import ReplyDrafter
from weibo_client import WeiboError, extract_bid, fetch_comments, fetch_post
from chat_judge import ChatJudge

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(TOOL_DIR, "config.json")

DEFAULT_CONFIG = {
    "judge": {"base_url": "https://openrouter.ai/api",
              "model": "typesafe/jev-1.13",
              "protocol": "auto",
              "api_key": ""},
    "reply": {"base_url": "https://openrouter.ai/api/v1",
              "model": "deepseek/deepseek-chat-v3.1", "api_key": ""},
    "weibo_cookie": "",
}

# base_url hosts that speak the Jev decisions protocol (/alpha/decisions or /v1/systemone)
JEV_PROVIDERS = ("openrouter", "bocha", "typesafe", "vercel", "opencode")


def load_config(path: str) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            for section in ("judge", "reply"):
                if isinstance(user_cfg.get(section), dict):
                    cfg[section].update(user_cfg[section])
            if user_cfg.get("weibo_cookie"):
                cfg["weibo_cookie"] = user_cfg["weibo_cookie"]
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[config] 无法读取 {path}: {exc}，使用默认配置", file=sys.stderr)
    else:
        print(f"[config] 未找到 {path}（可用 config.example.json 复制一份改名）",
              file=sys.stderr)

    env_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if env_key and not cfg["judge"]["api_key"]:
        cfg["judge"]["api_key"] = env_key
    if not cfg["reply"]["api_key"]:
        cfg["reply"]["api_key"] = cfg["judge"]["api_key"]
    env_cookie = (os.environ.get("WEIBO_COOKIE") or "").strip()
    if env_cookie and not cfg["weibo_cookie"]:
        cfg["weibo_cookie"] = env_cookie
    return cfg


def all_secrets(cfg: dict) -> list[str]:
    return [cfg["judge"].get("api_key", ""), cfg["reply"].get("api_key", ""),
            cfg.get("weibo_cookie", "")]


def read_post_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def looks_like_target(text: str) -> bool:
    """True when pasted input is a URL or id rather than post body text."""
    s = text.strip()
    if "weibo" in s.lower():
        return True
    return bool(re.fullmatch(r"[0-9A-Za-z]{6,20}", s))


def is_risky_post(judge_answers: dict) -> bool:
    """Trusted flag for the reply drafter: risk means candidates must never
    agree-with or amplify the post (unverified claims, rage bait, scams...)."""
    intent = judge_answers.get("post_intent") or {}
    credibility = judge_answers.get("credibility") or {}
    danger = judge_answers.get("danger_level") or {}
    if intent.get("choice") in ("spread_unverified", "drum_up_support", "troll_attack"):
        return True
    if credibility.get("choice") in ("plausible_unverified", "misleading_exaggeration",
                                     "clear_falsehood"):
        return True
    try:
        score = float(danger.get("score", 0))
        return score >= 4
    except (TypeError, ValueError):
        return False


def judge_call(judge: JevJudge, state: dict, questions: dict) -> tuple[dict, int]:
    started = time.monotonic()
    resp = judge.ask(state, questions)
    latency_ms = int((time.monotonic() - started) * 1000)
    return resp, latency_ms


def collect_answers(resp: dict, secrets: list[str] | None = None) -> dict:
    """Flatten answers into {question_key: answer}. Never fails the whole run."""
    answers = resp.get("answers")
    if not isinstance(answers, dict):
        raise JevError(f"Jev response has no 'answers' field: "
                       f"{redact_secrets(str(resp)[:200], secrets or [])}")
    return answers


def rank_replies(judge: JevJudge, state: dict, candidates: list[str],
                 secrets: list[str] | None = None) -> tuple[list[dict], int, dict]:
    question = build_rank_question(candidates)
    resp, latency_ms = judge_call(judge, state, question)
    answers = collect_answers(resp, secrets)
    best = answers.get("best_reply")
    if not isinstance(best, dict):
        raise JevError("Jev rank response has no best_reply answer")
    probs = best.get("probabilities") or {}
    keys = ("reply_a", "reply_b", "reply_c")
    ranked: list[dict] = []
    for key, text in zip(keys, candidates):
        try:
            prob = float(probs.get(key, 0.0))
        except (TypeError, ValueError):
            prob = 0.0
        ranked.append({"key": key, "text": text, "probability": prob})
    ranked.sort(key=lambda item: item["probability"], reverse=True)
    return ranked, latency_ms, resp


def sum_usage(*responses: dict) -> tuple[float | None, int, int]:
    """(cost_total or None, input_tokens, output_tokens) across responses.

    OpenAI-compatible providers report tokens but no dollar cost; Jev responses
    carry a cost field. Cost is None when no response reported one.
    """
    cost = 0.0
    has_cost = False
    tokens_in = tokens_out = 0
    for resp in responses:
        usage = resp.get("usage")
        if not isinstance(usage, dict):
            continue
        raw_cost = usage.get("cost")
        if raw_cost is not None:
            try:
                cost += float(raw_cost)
                has_cost = True
            except (TypeError, ValueError):
                pass
        for value in (usage.get("input_tokens"), usage.get("prompt_tokens")):
            if isinstance(value, (int, float)):
                tokens_in += int(value)
                break
        for value in (usage.get("output_tokens"), usage.get("completion_tokens")):
            if isinstance(value, (int, float)):
                tokens_out += int(value)
                break
    return (cost if has_cost else None), tokens_in, tokens_out


PROTOCOL_LABEL = {"jev": "Jev 专用判断模型", "chat": "聊天模型判断"}


def make_judge(cfg: dict, force: str = "auto"):
    """Pick the judge backend.

    judge.protocol wins unless `force` is given:
      auto  -> sniff base_url (Jev providers -> JevJudge, else ChatJudge)
      bocha -> Bocha Jev endpoint with the judge api_key
      ds    -> DeepSeek chat judge (uses reply.api_key first: that is where the
               DeepSeek key lives when the judge key belongs to Bocha)
      jev/chat -> force the backend on the configured base_url/model
    """
    judge_cfg = dict(cfg["judge"])
    protocol = (judge_cfg.get("protocol") or "auto").lower()
    force = (force or "auto").lower()
    if force == "bocha":
        judge_cfg["base_url"] = "https://jev.bocha.cn"
        judge_cfg["model"] = "bocha-jev-v1"
        protocol = "jev"
    elif force in ("ds", "deepseek"):
        judge_cfg["base_url"] = "https://api.deepseek.com"
        judge_cfg["model"] = "deepseek-flash"
        # the DeepSeek key lives in reply when the judge key belongs to Bocha
        judge_cfg["api_key"] = cfg["reply"].get("api_key", "") or judge_cfg["api_key"]
        protocol = "chat"
    elif force in ("jev", "chat"):
        protocol = force

    base = (judge_cfg.get("base_url") or "").lower()
    if protocol == "chat":
        backend = "chat"
    elif protocol == "jev":
        backend = "jev"
    else:
        backend = "jev" if any(p in base for p in JEV_PROVIDERS) else "chat"
    cls = JevJudge if backend == "jev" else ChatJudge
    judge = cls(judge_cfg["base_url"], judge_cfg["model"], judge_cfg["api_key"],
                secrets=all_secrets(cfg))
    judge.backend = backend
    return judge


def analyze_one(post: dict, cfg: dict, judge, perspective: str = "") -> tuple[dict, int]:
    """Judge -> draft -> rank for one post or comment. Returns (result, code)."""
    state = build_state(post["author"], post["text"],
                        post["retweeted_text"], post["context"])
    result = {
        "post": {k: v for k, v in post.items() if v},
        "state": state,
        "judge": {}, "replies": {},
        "cost": None, "tokens_in": 0, "tokens_out": 0,
        "total_latency_ms": 0,
    }
    started = time.monotonic()

    judge_resp, judge_ms = judge_call(judge, state, WEIBO_QUESTIONS)
    try:
        judge_answers = collect_answers(judge_resp, all_secrets(cfg))
    except JevError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return result, 1
    result["judge"] = {
        "endpoint": judge.url, "model": judge.model,
        "backend": getattr(judge, "backend", "jev"),
        "answers": judge_answers, "latency_ms": judge_ms,
    }

    responses = [judge_resp]
    try:
        drafter = ReplyDrafter(cfg["reply"]["base_url"], cfg["reply"]["model"],
                               cfg["reply"]["api_key"], secrets=all_secrets(cfg))
        draft_started = time.monotonic()
        candidates = drafter.draft(post["author"], post["text"],
                                   post["retweeted_text"], post["context"],
                                   skeptical=is_risky_post(judge_answers),
                                   perspective=perspective,
                                   is_comment=post.get("kind") == "comment")
        draft_ms = int((time.monotonic() - draft_started) * 1000)
        result["replies"] = {"model": drafter.model, "candidates": candidates,
                             "draft_latency_ms": draft_ms}
    except JevError as exc:
        result["replies"] = {"model": cfg["reply"]["model"], "candidates": [],
                             "draft_error": str(exc)}
        print(f"[警告] 候选回复草拟失败: {exc}", file=sys.stderr)

    if result["replies"].get("candidates"):
        try:
            ranked, rank_ms, rank_resp = rank_replies(
                judge, state, result["replies"]["candidates"], all_secrets(cfg))
            responses.append(rank_resp)
            result["replies"]["ranked"] = ranked
            result["replies"]["rank_latency_ms"] = rank_ms
        except JevError as exc:
            result["replies"]["rank_error"] = str(exc)
            print(f"[警告] 排序失败（按草拟顺序展示）: {exc}", file=sys.stderr)

    result["cost"], result["tokens_in"], result["tokens_out"] = sum_usage(*responses)
    result["total_latency_ms"] = int((time.monotonic() - started) * 1000)
    return result, 0


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def pick_comment_indices(n: int) -> list[int]:
    """Ask which comments to analyze. Accepts 'all', '1,3-5', or empty."""
    loops = 0
    while loops < 5:
        loops += 1
        line = input("分析哪些评论？（如 1,3-5 或 all，直接回车退出）: ").strip()
        if not line:
            return []
        if line.lower() in ("all", "全部"):
            return list(range(n))
        picked: list[int] = []
        try:
            for part in line.split(","):
                part = part.strip()
                if not part:
                    continue
                if "-" in part:
                    a, b = part.split("-", 1)
                    picked.extend(range(int(a) - 1, int(b)))
                else:
                    picked.append(int(part) - 1)
            picked = sorted({i for i in picked if 0 <= i < n})
        except ValueError:
            print("  格式示例:1,3-5 或 all", file=sys.stderr)
            continue
        if picked:
            return picked
        print("  没有选中的序号，重试。", file=sys.stderr)
    return []


def run_comments(page, post: dict, args: argparse.Namespace, cfg: dict, judge) -> int:
    """Fetch all comment levels of the post, drop the poster's own comments,
    and analyze how to reply to each remaining one."""
    print(f"[评论] 拉取帖子 {page.bid} 的全部评论（含二级回复）…", file=sys.stderr)
    try:
        comments = fetch_comments(page.bid, cfg.get("weibo_cookie", ""),
                                  max_comments=300)
    except WeiboError as exc:
        print(f"[抓取失败] {exc}", file=sys.stderr)
        return 1
    if not comments:
        print("[提示] 帖子没有评论，或评论接口被拦截。", file=sys.stderr)
        return 0

    poster_id = (page.author_id or "").strip()
    if poster_id:
        others = [c for c in comments if str(c.get("author_id", "")) != str(poster_id)]
        mine = len(comments) - len(others)
        print(f"[评论] 共 {len(comments)} 条（一级 {sum(1 for c in comments if c['level'] == 1)}"
              f" + 二级 {sum(1 for c in comments if c['level'] == 2)}），"
              f"剔除楼主自己的 {mine} 条，待分析 {len(others)} 条。", file=sys.stderr)
    else:
        others = comments
        print(f"[评论] 共 {len(comments)} 条（无法确定楼主 uid，不过滤）。", file=sys.stderr)
    if not others:
        print("[提示] 除楼主外没有其它评论，无需分析。", file=sys.stderr)
        return 0

    parent_map = {c["idstr"]: c for c in comments}

    if args.comments == "pick":
        for i, c in enumerate(others, 1):
            level_tag = "一级" if c["level"] == 1 else f"二级(回复 {parent_map.get(c['parent'], {}).get('author', '?')})"
            print(f"  {i}. [{level_tag}] [{c['author'] or '?'}] {_truncate(c['text'], 32)}",
                  file=sys.stderr)
        selected_idx = pick_comment_indices(len(others))
        if not selected_idx:
            print("未选择，退出。")
            return 0
        selected = [others[i] for i in selected_idx]
    elif str(args.comments).lower() in ("all", "全部"):
        selected = others
    else:
        try:
            n = max(1, min(int(args.comments), len(others)))
        except (TypeError, ValueError):
            print(f"[错误] --comments 参数应为条数、all,或不带值进入挑选模式: "
                  f"got {args.comments!r}", file=sys.stderr)
            return 1
        selected = others[:n]

    context_base = (f"原帖(作者 {post['author'] or '?'}): {_truncate(post['text'], 300)}")
    perspective = ""
    if getattr(args, "as_poster", False):
        perspective = ("你是这条评论所在原帖的作者，现在正以原作者的视角和口吻"
                       "回复这条评论，可以直接提到自己原帖里的内容。")
    failed = 0
    results = []
    for idx, c in enumerate(selected, 1):
        level_tag = "一级评论" if c["level"] == 1 else "二级回复"
        parent = parent_map.get(c.get("parent") or "")
        relation = (f"这条{level_tag}回复的是微博帖子。{context_base}")
        if parent:
            relation += (f" 它直接回复的一级评论是（作者 {parent.get('author', '?')}）："
                         f"{_truncate(parent.get('text', ''), 200)}")
        print(f"[分析] {idx}/{len(selected)} {level_tag} 由 {c['author'] or '?'}: "
              f"{_truncate(c['text'], 24)}", file=sys.stderr)
        cpost = {
            "author": c["author"], "text": c["text"],
            "retweeted_text": "",
            "context": relation,
            "source": f"{level_tag} {c['idstr']} @ 帖子 {page.bid}",
            "kind": "comment",
        }
        try:
            result, code = analyze_one(cpost, cfg, judge, perspective=perspective)
        except JevError as exc:
            failed += 1
            results.append({"comment": c, "error": str(exc)})
            print(f"[错误] {idx} 分析失败: {exc}", file=sys.stderr)
            continue
        if code != 0:
            failed += 1
        results.append({"comment": {"idstr": c["idstr"], "author": c["author"],
                                    "text": c["text"], "level": c["level"],
                                    "parent": c.get("parent")},
                        "result": result})
        if not args.json:
            from panel import render
            print(render(result))

    if args.json:
        print(json.dumps({"post": {"bid": page.bid, "author": post["author"],
                                   "author_id": page.author_id,
                                   "text": post["text"]},
                          "comments": results},
                         ensure_ascii=False, indent=2))
    return 1 if failed == len(results) else 0


def run(args: argparse.Namespace, cfg: dict) -> int:
    post = {"author": "", "text": "", "retweeted_text": "", "context": "", "source": "手动输入"}
    raw_dump: dict | None = None
    page = None

    # ---- 1. obtain the post ----
    if args.url:
        bid = extract_bid(args.url)
        page = fetch_post(bid, cfg.get("weibo_cookie", ""))
        post.update(author=page.author, text=page.text,
                    retweeted_text=page.retweeted_text,
                    source=page.source, bid=page.bid)
        raw_dump = page.raw
    elif args.file:
        post["text"] = read_post_text(args.file)
        post["author"] = args.author or ""
        post["source"] = f"文件:{os.path.basename(args.file)}"
    elif args.text:
        post["text"] = args.text.strip()
        post["author"] = args.author or ""
    else:  # interactive
        print("粘贴微博链接 / 帖子ID，或直接粘贴帖子正文（多行结尾按两下回车）：", flush=True)
        lines: list[str] = []
        while True:
            line = input()
            if line == "" and lines and lines[-1] == "":
                break
            lines.append(line)
        pasted = "\n".join(lines).strip()
        if not pasted:
            print("未输入任何内容，退出。")
            return 1
        if looks_like_target(pasted):
            bid = extract_bid(pasted)
            page = fetch_post(bid, cfg.get("weibo_cookie", ""))
            post.update(author=page.author, text=page.text,
                        retweeted_text=page.retweeted_text,
                        source=page.source, bid=page.bid)
            raw_dump = page.raw
        else:
            post["text"] = pasted
            post["author"] = input("作者昵称（可空）: ").strip() or ""

    post["context"] = (args.context or "").strip()
    if not post["text"]:
        print("帖子内容为空，无法分析。", file=sys.stderr)
        return 1

    if raw_dump is not None:
        print(" [抓取成功]", post["source"], "| 作者:", post["author"] or "?",
              "| 正文:", post["text"][:40].replace("\n", " ") + "…", file=sys.stderr)

    if args.dump:
        if raw_dump is None:
            print("--dump 需要 --url 或贴链接输入（没有抓取内容可打印）", file=sys.stderr)
            return 1
        print(redact_secrets(json.dumps(raw_dump, ensure_ascii=False, indent=2),
                             all_secrets(cfg)))
        return 0

    if args.payload:
        state = build_state(post["author"], post["text"],
                            post["retweeted_text"], post["context"])
        print(json.dumps({"state": state, "questions": WEIBO_QUESTIONS},
                         ensure_ascii=False, indent=2))
        return 0

    # ---- 2. judge backend (one judge instance for the whole run) ----
    try:
        judge = make_judge(cfg, force=getattr(args, "judge", "auto"))
    except JevError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    # ---- comments mode: analyze how to reply to each first-level comment ----
    if args.comments is not None:
        if page is None:
            print("[错误] --comments 需要帖子链接（--url 或贴链接），才能拉取评论区。",
                  file=sys.stderr)
            return 1
        return run_comments(page, post, args, cfg, judge)

    # ---- 3. single-post flow ----
    result, code = analyze_one(post, cfg, judge)
    if code != 0:
        return code
    if args.json:
        from panel import render_json
        print(render_json(result))
    else:
        from panel import render
        print(render(result))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="微博帖子分析:判断意图/风险/可信度/攻击性并给出 3 条候选回复")
    parser.add_argument("--text", help="直接分析这段帖子正文")
    parser.add_argument("--file", help="从 UTF-8 文本文件读帖子正文")
    parser.add_argument("--url", help="微博链接或帖子 ID（自动抓取正文）")
    parser.add_argument("--author", help="帖子作者昵称（粘贴输入时用）")
    parser.add_argument("--context", help="额外上下文（如原帖内容）")
    parser.add_argument("--config", default=CONFIG_PATH,
                        help="配置文件路径（默认 tools/weibo/config.json）")
    parser.add_argument("--judge", choices=["auto", "bocha", "ds"], default="auto",
                        help="判断后端:auto=按配置,bocha=博查 Jev,ds=DeepSeek 聊天判断"
                             "(ds 用 reply 的密钥)")
    parser.add_argument("--json", action="store_true",
                        help="输出完整 JSON 结果（含原始 answers）")
    parser.add_argument("--comments", nargs="?", const="pick",
                        metavar="N|all|pick",
                        help="拉取帖子的一级评论并逐条分析怎么回：N 条 / all / "
                             "不带值=列表挑选。需配合 --url 使用")
    parser.add_argument("--as-poster", action="store_true",
                        help="配合 --comments：候选评论以原帖作者的口吻编写"
                             "（自己回复自己帖子下的评论时用）")
    parser.add_argument("--payload", action="store_true",
                        help="只打印将发送的 state+questions JSON，不调 API")
    parser.add_argument("--dump", action="store_true",
                        help="只抓取并打印帖子原始 JSON，不调模型（验证 Cookie 用）")
    args = parser.parse_args()

    cfg = load_config(args.config)
    try:
        return run(args, cfg)
    except WeiboError as exc:
        print(f"[抓取失败] {exc}", file=sys.stderr)
        return 1
    except JevError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130


if __name__ == "__main__":
    sys.exit(main())