"""Weibo post fetching.

Primary route is https://weibo.com/ajax/statuses/show?id={bid} with the user's own
cookie (same endpoint family as the scraper reference weibo_scraper.py). If that
fails or no cookie is configured, falls back to https://m.weibo.cn/statuses/show
which works without login for many public posts.

Only public, single-post fetching for local analysis: one request per run, retried
twice with backoff. No crawling fan-out.
"""

from __future__ import annotations

import html as html_lib
import json
import random
import re
import time

import requests


class WeiboError(Exception):
    pass


class WeiboPage:
    """Parsed weibo post suitable for analysis."""

    def __init__(self, author: str, author_id: str, text: str,
                 retweeted_text: str, raw: dict, source: str, bid: str) -> None:
        self.author = author or ""
        self.author_id = author_id or ""
        self.text = text
        self.retweeted_text = retweeted_text
        self.raw = raw
        self.source = source
        self.bid = bid

    def summary(self) -> str:
        text = self.text.strip().replace("\n", " ")
        return text[:40] + ("…" if len(text) > 40 else "")


_ID_PATTERNS = [
    (r"/detail/([0-9A-Za-z]+)", 1),
    (r"/status/([0-9A-Za-z]+)", 1),
    (r"/(\d+)/([0-9A-Za-z]+)(?:\?|/|$)", 2),
    (r"/([0-9A-Za-z]{8,20})(?:\?|/|$)", 1),
]


def extract_bid(text: str) -> str:
    """Pull the post id (bid) from a weibo URL or a bare id string."""
    s = text.strip()
    if not s:
        raise WeiboError("empty input: no url or post id")
    if "weibo" in s.lower():
        for pattern, group in _ID_PATTERNS:
            m = re.search(pattern, s)
            if m:
                return m.group(group)
        raise WeiboError(f"cannot find a post id in url: {s[:60]}")
    if re.fullmatch(r"[0-9A-Za-z]{6,20}", s):
        return s
    raise WeiboError(
        "input looks like neither a weibo url nor a post id. "
        "Paste the full link (weibo.com/xxx/yyy or m.weibo.cn/detail/yyy) or the bare id."
    )


_DESKTOP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://weibo.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "X-Requested-With": "XMLHttpRequest",
}

_MOBILE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
    ),
    "Referer": "https://m.weibo.cn/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
}


def _strip_html(text: str) -> str:
    """HTML -> plain text: newline for <br>, remove tags, unescape, drop urls."""
    if not text:
        return ""
    out = re.sub(r"<br\s*/?>", "\n", text)
    out = re.sub(r"<[^>]+>", "", out)
    out = html_lib.unescape(out)
    out = re.sub(r"https?://\S+|//t\.cn/\S+", "", out)
    out = out.replace("\u200b", "")
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r" *\n *", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


_PAGE_KEYS = ("longTextContent", "text_raw", "text")


def _pick_text(data: dict) -> str:
    for key in _PAGE_KEYS:
        value = data.get(key)
        if value:
            text = _strip_html(str(value))
            if text:
                return text
    return ""


def _parse_desktop(body: dict, bid: str, raw: dict) -> WeiboPage:
    # /ajax/statuses/show returns the post object directly (no ok/data envelope);
    # accept both that shape and a data-wrapped one.
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    if not isinstance(data, dict) or not (data.get("idstr") or data.get("id")):
        raise WeiboError("ajax/statuses/show returned an unexpected payload")
    user = data.get("user") or {}
    author = user.get("screen_name", "")
    author_id = str(user.get("idstr") or user.get("id") or "")
    text = _pick_text(data)
    if not text:
        raise WeiboError("post has no readable text (maybe a pure-image post?)")
    retweeted = ""
    rt = data.get("retweeted_status") or {}
    if rt:
        retweeted = _pick_text(rt)
    return WeiboPage(author, author_id, text, retweeted, raw,
                     "weibo.com/ajax/statuses/show", bid)


def _parse_mobile(body: dict, bid: str, raw: dict) -> WeiboPage:
    data = body.get("data") or {}
    if not data:
        raise WeiboError("m.weibo.cn/statuses/show returned ok=1 but empty data")
    user = data.get("user") or {}
    author = user.get("screen_name", "")
    author_id = str(user.get("idstr") or user.get("id") or "")
    text = _pick_text(data)
    if not text:
        raise WeiboError("post has no readable text (maybe a pure-image post?)")
    retweeted = ""
    rt = data.get("retweeted_status") or {}
    if rt:
        retweeted = _pick_text(rt)
    return WeiboPage(author, author_id, text, retweeted, raw,
                     "m.weibo.cn/statuses/show (guest)", bid)


def _get_json(url: str, params: dict, headers: dict, timeout: int = 30) -> dict:
    """One JSON GET with two retries and 2-4s backoff."""
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code >= 500:
                raise WeiboError(f"{url} HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            time.sleep(2 + 2 * attempt)
        except requests.HTTPError as exc:
            raise WeiboError(f"{url} HTTP {exc.response.status_code}") from None
        except (json.JSONDecodeError, ValueError) as exc:
            raise WeiboError(f"{url} returned non-JSON (likely blocked; check cookie)") from None
    raise WeiboError(f"{url} unreachable after 3 attempts: {last_exc}") from None


def fetch_post(bid: str, cookie: str = "") -> WeiboPage:
    """Fetch one post by id. Uses the cookie when provided, else tries guest route."""
    errors: list[str] = []

    desktop_headers = dict(_DESKTOP_HEADERS)
    if cookie:
        desktop_headers["Cookie"] = cookie
    try:
        body = _get_json("https://weibo.com/ajax/statuses/show",
                         {"id": bid}, desktop_headers)
        # success shapes: data-wrapped {"ok":1,"data":{...}} or bare post object
        if body.get("ok") == 1 or isinstance(body.get("data"), dict) \
                or ("id" in body and ("text_raw" in body or "text" in body)):
            return _parse_desktop(body, bid, body)
        errors.append(f"ajax endpoint ok!={body.get('ok')}: {str(body)[:120]}")
    except (WeiboError, requests.RequestException) as exc:
        errors.append(str(exc))

    # Guest fallback: no cookie needed for many public posts.
    try:
        body = _get_json("https://m.weibo.cn/statuses/show",
                         {"id": bid}, dict(_MOBILE_HEADERS))
        if body.get("ok") == 1:
            return _parse_mobile(body, bid, body)
        errors.append(f"mobile endpoint ok!={body.get('ok')}: {str(body)[:120]}")
    except (WeiboError, requests.RequestException) as exc:
        errors.append(str(exc))

    detail = " | ".join(e for e in errors if e)
    raise WeiboError(
        f"failed to fetch post {bid}. "
        f"If the post exists and is public, configure weibo_cookie in "
        f"tools/weibo/config.json (copy the Cookie header from a logged-in browser). "
        f"Details: {detail[:240]}"
    )


_COMMENTS_URL = "https://weibo.com/ajax/statuses/buildComments"
_MOBILE_COMMENTS_URL = "https://m.weibo.cn/comments/hotflow"


def _comment_of(item: dict) -> dict | None:
    """Normalize one comment/reply item into
    {idstr, author, author_id, text, created_at}."""
    if not isinstance(item, dict):
        return None
    text = _pick_text(item)
    if not text:
        return None
    user = item.get("user") or {}
    return {
        "idstr": str(item.get("idstr") or item.get("id") or ""),
        "author": user.get("screen_name", ""),
        "author_id": str(user.get("idstr") or user.get("id") or ""),
        "text": text,
        "created_at": item.get("created_at", ""),
    }


def _comments_page(url: str, params: dict, headers: dict) -> tuple[list[dict], list[dict], int]:
    """One comments request -> (normalized items, raw items, next max_id)."""
    body = _get_json(url, params, headers)
    if body.get("ok") != 1:
        raise WeiboError(f"comments endpoint ok!={body.get('ok')}: "
                         f"{str(body)[:120]}")
    raw_items = [i for i in (body.get("data") or []) if isinstance(i, dict)]
    items = [c for c in (_comment_of(i) for i in raw_items) if c]
    return items, raw_items, body.get("max_id") or 0


def fetch_comments(post_id: str, cookie: str = "", max_comments: int = 300,
                   max_threads: int = 0) -> list[dict]:
    """All comment levels of one post (top-level + nested replies), newest/hot first.

    Each returned dict carries `level` (1 = 一级评论, 2 = 二级回复) and `parent`
    (idstr of the top-level comment a level-2 reply hangs under, None for level 1).
    `max_threads` > 0 limits how many top-level comments get their own
    fetch_level=1 pagination (0 = all) — bounds the request count on hot posts.
    Pagination is polite (2-3s random sleep between requests); falls back to the
    mobile hotflow route (single page, top-level only) if the desktop route fails.
    """
    headers = dict(_DESKTOP_HEADERS)
    if cookie:
        headers["Cookie"] = cookie

    collected: list[dict] = []
    known: set[str] = set()

    def add(comment: dict, level: int, parent: str | None) -> None:
        if comment["idstr"] in known or len(collected) >= max_comments:
            return
        known.add(comment["idstr"])
        collected.append({**comment, "level": level, "parent": parent})

    try:
        # -- top-level pagination (embedded nested replies come along) --
        page, max_id = 1, 0
        while len(collected) < max(1, min(max_comments, 300)):
            params = {
                "id": post_id,
                "is_reload": 1 if page == 1 else 0,
                "is_show_bulletin": 2,
                "is_mix": 0,
                "count": 20,
                "fetch_level": 0,
                "locale": "zh-CN",
                "max_id": max_id,
            }
            items, raw_items, next_max = _comments_page(_COMMENTS_URL, params, headers)
            for item in items:
                add(item, 1, None)
            # embedded secondary replies inside top-level items
            for raw_item in raw_items:
                for sub in raw_item.get("comments") or []:
                    sub_c = _comment_of(sub)
                    if sub_c:
                        add(sub_c, 2, str(raw_item.get("idstr") or raw_item.get("id") or ""))
            if not items or next_max == 0:
                break
            max_id, page = next_max, page + 1
            time.sleep(random.uniform(2, 3))

        # -- full nested replies per top-level comment (fetch_level=1) --
        top_ids = [c["idstr"] for c in collected if c["level"] == 1]
        for tid in top_ids[:max_threads] if max_threads else top_ids:
            if len(collected) >= max_comments:
                break
            reply_max, rpage = 0, 1
            while len(collected) < max_comments:
                params = {
                    "id": tid,
                    "is_reload": 1 if rpage == 1 else 0,
                    "is_show_bulletin": 2,
                    "count": 20,
                    "fetch_level": 1,
                    "locale": "zh-CN",
                    "max_id": reply_max,
                }
                items, _, next_max = _comments_page(_COMMENTS_URL, params, headers)
                for item in items:
                    add(item, 2, tid)
                if not items or next_max == 0:
                    break
                reply_max, rpage = next_max, rpage + 1
                time.sleep(random.uniform(2, 3))
            time.sleep(random.uniform(2, 3))
        return collected
    except (WeiboError, requests.RequestException):
        pass  # fall back to the mobile guest route below

    # Mobile fallback: one page of hot comments, no login needed for many posts.
    body = _get_json(_MOBILE_COMMENTS_URL,
                     {"id": post_id, "mid": post_id, "max_id_type": 0},
                     dict(_MOBILE_HEADERS))
    if body.get("ok") != 1:
        raise WeiboError(f"mobile comments endpoint ok!={body.get('ok')}: "
                         f"{str(body)[:120]}")
    data = (body.get("data") or {}).get("data") or []
    for item in data:
        comment = _comment_of(item)
        if comment:
            add(comment, 1, None)
        for sub in item.get("comments") or []:
            sub_c = _comment_of(sub)
            if sub_c:
                add(sub_c, 2, str(item.get("id") or item.get("idstr") or ""))
    return collected