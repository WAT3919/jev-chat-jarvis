#!/usr/bin/env python3
"""两个用户在某博主帖子下的互动时间线。

给定用户 A 和 B(uid、/u/ 链接,或昵称——昵称解析可能需要登录态),扫描 A 的
最近帖子(按页,跳过无评论帖),把每帖评论楼里「B 的任何评论 + A 在含 B 评论的
楼里的回复」收集起来,按时间排序,输出最近 --limit 条。

用法:
  python interactions.py 5979979070 1234567890                # uid 或 /u/ 链接
  python interactions.py https://weibo.com/u/5979979070 "@某人" --pages 10
  python interactions.py A B --limit 100 --json

礼貌限频:帖列表翻页与评论翻页各自随机休眠 2-3s;建议别连续扫太多页,见 README
「风控」段。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOL_DIR)

import analyze
from weibo_client import (WeiboError, _comment_of, _DESKTOP_HEADERS,
                          _strip_html, fetch_comments)
import requests

_MYBLOG_URL = "https://weibo.com/ajax/statuses/mymblog"
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_CN_TZ = timezone(timedelta(hours=8))


def _headers(cookie: str) -> dict:
    headers = dict(_DESKTOP_HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    return headers


def parse_ts(raw) -> datetime | None:
    """Parse weibo ish timestamps like 'Sun Mar 08 18:01:15 +0800 2026'
    (locale-proof; falls back to ISO-ish / unix)."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and 1e9 < raw < 4e9:
        return datetime.fromtimestamp(raw, tz=_CN_TZ)
    s = str(raw).strip()
    if not s or s in ("刚刚",):
        return None
    m = re.match(r"(?:\w{3}\s+)?(\w{3})\s+(\d{1,2})\s+(\d{1,2}:\d{2}:\d{2})\s+([+-]\d{4})\s+(\d{4})", s)
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            hh, mm, ss = map(int, m.group(3).split(":"))
            off = m.group(4)
            sign = 1 if off[0] == "+" else -1
            tz = timezone(sign * timedelta(hours=int(off[1:3]), minutes=int(off[3:5])))
            return datetime(int(m.group(5)), month, int(m.group(2)),
                            hh, mm, ss, tzinfo=tz)
    candidate = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def _search_user_by_name(name: str, cookie: str) -> list[dict]:
    """s.weibo.com user search -> [{uid, screen_name, avatar, desc, verified}].
    Needs a login cookie; the old ajax/user/query endpoint is gone (404) and
    guest routes are blocked."""
    try:
        resp = requests.get("https://s.weibo.com/user", params={"q": name},
                            headers=_headers(cookie), timeout=20)
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        raise WeiboError(f"昵称搜索失败({exc});请改用 uid 或 /u/ 链接") from None
    results: list[dict] = []
    for card in re.split(r"card card-user-b", html)[1:]:
        uid_m = re.search(r"weibo\.com/u/(\d+)", card)
        name_m = re.search(r'class="name"[^>]*>([^<]+)<', card)
        if not (uid_m and name_m):
            continue
        avatar_m = re.search(r'src="(https://[^"]+?\.(?:jpg|png|jpeg)[^"]*)"', card)
        desc_m = re.search(r"<p[^>]*>(.*?)</p>", card)
        desc = re.sub(r"<[^>]+>|\s+", " ", desc_m.group(1)).strip() if desc_m else ""
        results.append({
            "uid": uid_m.group(1),
            "screen_name": name_m.group(1).strip(),
            "avatar": avatar_m.group(1) if avatar_m else "",
            "desc": desc[:60],
            "verified": ("woo-icon--vyellow" in card or "woo-icon--vblue" in card),
        })
    return results


def search_user_api(query: str, cookie: str) -> list[dict]:
    """Public helper for the web UI: resolve uid/`/u/` directly, else nickname
    search with full candidate info (up to 20)."""
    s = (query or "").strip().strip("@")
    m = re.search(r"/u/(\d+)", s)
    if m or re.fullmatch(r"\d{4,15}", s):
        uid = m.group(1) if m else s
        return [{"uid": uid, "screen_name": "", "avatar": "", "desc": "",
                 "verified": False, "exact": True}]
    if not cookie:
        raise WeiboError(
            f"昵称「{s}」搜索需要登录态:请在 config.json 配置 weibo_cookie,"
            f"或直接填 uid / weibo.com/u/… 链接")
    results = _search_user_by_name(s, cookie)
    if not results:
        raise WeiboError(f"昵称「{s}」搜索无结果;请确认昵称或改用 uid / /u/ 链接")
    for r in results:
        r["exact"] = (r["screen_name"] == s)
    return results


def resolve_uid(text: str, cookie: str) -> dict:
    """Resolve '5979979070' / '/u/5979979070' / '/n/昵称' / 昵称 into
    {uid, screen_name, exact}. Nickname search prefers an exact screen-name
    match; exact=False means we picked the closest search hit."""
    s = (text or "").strip().strip("@")
    m = re.search(r"/u/(\d+)", s)
    if m:
        return {"uid": m.group(1), "screen_name": "", "exact": True}
    if re.fullmatch(r"\d{4,15}", s):
        return {"uid": s, "screen_name": "", "exact": True}
    m = re.search(r"/n/([^/?#]+)", s)
    name = m.group(1) if m else s
    if not name:
        raise WeiboError(f"无法解析用户标识: {text!r}")
    if not cookie:
        raise WeiboError(
            f"昵称「{name}」需要登录态搜索:请在 config.json 配置 weibo_cookie,"
            f"或直接填 uid / weibo.com/u/… 链接")
    results = _search_user_by_name(name, cookie)
    if not results:
        raise WeiboError(f"昵称「{name}」搜索无结果;请确认昵称或改用 uid / /u/ 链接")
    exact = next((r for r in results if r["screen_name"] == name), None)
    if exact:
        return {"uid": exact["uid"], "screen_name": exact["screen_name"], "exact": True}
    first = results[0]
    return {"uid": first["uid"], "screen_name": first["screen_name"], "exact": False}


def fetch_user_posts(uid: str, cookie: str, pages: int,
                     include_top: bool = False, progress=None) -> list[dict]:
    """Recent original posts of uid via mymblog (retweets and pinned posts
    filtered out unless include_top). Pinned posts carry isTop=1/mblogtype=2
    and their old interactions would stretch the observation window and skew
    the goodwill baseline, so they are skipped by default."""
    _p = progress or (lambda m: None)
    posts: list[dict] = []
    for page in range(1, pages + 1):
        try:
            resp = requests.get(_MYBLOG_URL,
                                params={"uid": uid, "page": page, "feature": 0},
                                headers=_headers(cookie), timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise WeiboError(f"拉取帖子列表失败(第 {page} 页): {exc}") from None
        if data.get("ok") != 1:
            raise WeiboError(f"mymblog ok!={data.get('ok')}: Cookie 可能失效或目标不可见 "
                             f"{str(data)[:100]}")
        items = data.get("data", {}).get("list") or []
        for p in items:
            if not isinstance(p, dict):
                continue
            if str((p.get("user") or {}).get("idstr", "")) != str(uid):
                continue  # retweets
            if not p.get("idstr"):
                continue
            if (p.get("isTop") or p.get("mblogtype") == 2) and not include_top:
                _p(f"[跳过] 置顶帖 {p['idstr']}(互动过早,避免拉长观察窗口误判)")
                continue
            posts.append({
                "idstr": str(p["idstr"]),
                "text": _strip_html(p.get("text_raw") or p.get("text") or ""),
                "created_at": p.get("created_at", ""),
                "comments_count": int(p.get("comments_count") or 0),
            })
        if not items:
            break
        if page < pages:
            time.sleep(2 + (time.time() % 1))  # 2-3s
    return posts


def collect_interactions(uid_a: str, uid_b: str, cookie: str,
                         pages: int, limit: int,
                         progress=print, include_top: bool = False) -> dict:
    posts = fetch_user_posts(uid_a, cookie, pages,
                             include_top=include_top, progress=progress)
    progress(f"[互动] {uid_a} 的帖子共抓到 {len(posts)} 条(含置顶),开始翻评论区…")
    hits: list[dict] = []
    scanned = 0
    for idx, post in enumerate(posts, 1):
        if post["comments_count"] <= 0:
            continue
        scanned += 1
        progress(f"[进度] 第 {idx}/{len(posts)} 帖 · {post['idstr']}"
                 f"({post['comments_count']} 评论) · 已命中 {len(hits)}")
        try:
            comments = fetch_comments(post["idstr"], cookie,
                                      max_comments=100, max_threads=30)
        except WeiboError as exc:
            progress(f"[警告] 帖 {post['idstr']} 评论抓取失败: {exc}")
            continue
        threads: dict[str, list[dict]] = {}
        for c in comments:
            root = c["idstr"] if c["level"] == 1 else (c.get("parent") or "")
            if not root:
                continue
            threads.setdefault(root, []).append(c)
        for root, items in threads.items():
            uid_set = {str(c.get("author_id", "")) for c in items}
            if str(uid_b) not in uid_set:
                continue  # B 没参与的楼,跳过
            for c in items:
                if str(c.get("author_id", "")) not in (str(uid_a), str(uid_b)):
                    continue
                ts = parse_ts(c.get("created_at"))
                hits.append({
                    "ts": ts.isoformat() if ts else None,
                    "_dt": ts,
                    "who": "A" if str(c.get("author_id", "")) == str(uid_a) else "B",
                    "author": c.get("author", ""),
                    "text": c.get("text", ""),
                    "level": c["level"],
                    "post_id": post["idstr"],
                    "post_summary": post["text"][:40],
                })
        if len(hits) >= limit * 2:  # some slack, then trim precisely at the end
            progress(f"[互动] 已收集 {len(hits)} 条,提前停止扫描(第 {idx}/{len(posts)} 帖)。")
            break
    hits.sort(key=lambda h: (h["_dt"] is None, h["_dt"] or datetime.min.replace(tzinfo=_CN_TZ)))
    kept = hits[-limit:]
    progress(f"[互动] 共命中 {len(hits)} 条,输出最近 {len(kept)} 条。")
    return {"a_uid": uid_a, "b_uid": uid_b, "posts_found": len(posts),
            "posts_scanned": scanned, "total_hits": len(hits),
            "interactions": kept}


def print_table(bundle: dict) -> None:
    print("=" * 72)
    a_shown = f"{bundle.get('a_name') or ''}/{bundle['a_uid']}"
    b_shown = f"{bundle.get('b_name') or ''}/{bundle['b_uid']}"
    print(f" A({a_shown}) 与 B({b_shown}) 的互动时间线"
          f" · 扫描 {bundle['posts_scanned']} 帖 / 命中 {bundle['total_hits']} 条,"
          f"取最近 {len(bundle['interactions'])} 条")
    print("=" * 72)
    last_post = None
    for h in bundle["interactions"]:
        if h["post_id"] != last_post:
            text = h["post_summary"].replace("\n", " ").replace("  ", " ")
            print(f"\n[帖 {h['post_id']}] {text[:46]}")
            print("-" * 72)
            last_post = h["post_id"]
        who = "A" if h["who"] == "A" else "B"
        when = (datetime.fromisoformat(h["ts"]).astimezone(_CN_TZ)
                .strftime("%m-%d %H:%M") if h["ts"] else "时间未知")
        mark = "◆" if h["who"] == "A" else " "
        print(f" {when} {mark}{who} [{h['author'] or '?'}] {h['text'][:120]}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="列出用户 A 帖子下 A、B 两人最近的互动(按时间排序)")
    parser.add_argument("user_a", help="用户 A:uid、/u/ 链接、/n/ 链接或昵称")
    parser.add_argument("user_b", help="用户 B:同上")
    parser.add_argument("--pages", type=int, default=10,
                        help="最多翻 A 的多少页帖子(每页约20条,默认10)")
    parser.add_argument("--limit", type=int, default=100,
                        help="最多输出多少条互动(默认100)")
    parser.add_argument("--include-top", action="store_true",
                        help="包含置顶帖(默认跳过:置顶帖的互动往往很早,会拉长"
                             "观察窗口,让频率/时间衰减失真)")
    parser.add_argument("--config", default=analyze.CONFIG_PATH,
                        help="配置文件路径(读 weibo_cookie)")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    cfg = analyze.load_config(args.config)
    cookie = (cfg.get("weibo_cookie") or "").strip()
    if not cookie:
        print("[提示] 未配置 Cookie,改用游客模式(可能拿不到帖子列表)。",
              file=sys.stderr)
    try:
        ra = resolve_uid(args.user_a, cookie)
        rb = resolve_uid(args.user_b, cookie)
        if ra["uid"] == rb["uid"]:
            print("[错误] A 和 B 是同一个用户。", file=sys.stderr)
            return 1
        for tag, r in (("A", ra), ("B", rb)):
            shown = r["screen_name"] or "(uid)"
            if r["exact"]:
                print(f"[用户] {tag}={shown}({r['uid']})", file=sys.stderr)
            else:
                print(f"[警告] {tag} 没有精确匹配的昵称,取搜索首条 {shown}({r['uid']});"
                      f"如不对请改用 uid / /u/ 链接", file=sys.stderr)
        bundle = collect_interactions(ra["uid"], rb["uid"], cookie,
                                      args.pages, args.limit,
                                      progress=lambda m: print(m, file=sys.stderr),
                                      include_top=args.include_top)
        bundle["a_name"], bundle["b_name"] = ra["screen_name"], rb["screen_name"]
    except WeiboError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    if args.json:
        out = [h for h in bundle["interactions"]]
        for h in out:
            h.pop("_dt", None)
        bundle["interactions"] = out
        print(json.dumps(bundle, ensure_ascii=False, indent=2))
    else:
        print_table(bundle)
    return 0


if __name__ == "__main__":
    sys.exit(main())