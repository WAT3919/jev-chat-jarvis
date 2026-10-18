#!/usr/bin/env python3
"""微博分析 — 本地图形界面(纯标准库实现,不需要 Flask)。

启动:
  启动分析界面.bat          # 双击(推荐,见文件)
  py webui.py               # Windows 终端(cmd/PowerShell)推荐用 py 启动器
  python webui.py           # Git Bash / 依赖齐全的解释器
  python webui.py --port 9000 --no-browser

页面功能与 CLI 一致,两个标签页:
  1. 帖子分析:贴链接或文本 → 判断 7 维 → 3 条候选回复排序;
     可开「评论区」模式(自动剔除楼主、覆盖全部层级),可选楼主口吻与判断后端。
  2. 互动时间线:指定用户 A、B,列出 A 帖子下两人最近的互动(按时间排序)。
配置读取 tools/weibo/config.json(与 CLI 共用);密钥不出现在任何页面/接口输出里。
仅依赖 requests(weibo_client 需要);HTTP 部分用标准库 http.server。
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import subprocess
import sys
import tempfile
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOL_DIR)

import analyze
import interactions
from chat_judge import _extract_json_object
from jev_client import JevError
from reply_client import ReplyDrafter
from weibo_client import WeiboError, extract_bid, fetch_comments, fetch_post

SERVER_PORT = 8770  # set in main(); the export PDF renderer needs it

# ---- background scan jobs for the interactions timeline ----
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_MAX_JOBS = 20


def _job_progress(job: dict):
    def progress(msg: str) -> None:
        with _JOBS_LOCK:
            job["logs"].append(msg)
    return progress


def _prune_jobs() -> None:
    finished = [k for k, v in _JOBS.items() if v["status"] != "running"]
    for k in finished[:max(0, len(_JOBS) - _MAX_JOBS + 1)]:
        _JOBS.pop(k, None)


_COURTESY_WORDS = ("踩踩", "谢谢", "支持", "加油", "沙发", "打卡", "路过",
                   "点赞", "666", "来了")


def relationship_signals(bundle: dict) -> dict:
    """Objective, conservative goodwill baseline from observable signals.

    Deliberately harsh: low frequency, courtesy-only small talk, and months of
    silence cannot produce high scores. The model may only adjust ±15 on top.
    """
    inter = bundle.get("interactions") or []
    n = len(inter)
    now = datetime.now(interactions._CN_TZ)
    times = sorted(datetime.fromisoformat(h["ts"]) for h in inter if h.get("ts"))
    span_days = max((times[-1] - times[0]).days, 1) if len(times) >= 2 else 1
    # 频率窗口 = 首条互动到现在(不是首条到末条!集中两天的互动不代表月均活跃)
    window_days = max((now - times[0]).days, 1)
    days_since_last = max((now - times[-1]).days, 0)
    window_months = max(window_days / 30.0, 0.5)
    freq = n / window_months
    # 时间衰减:30 天内不衰减,之后线性衰减到 0.35 封底(7 个月没互动 ≈ 0.67,一年以上 0.35)
    decay = max(0.35, 1.0 - max(0, days_since_last - 30) / 365 * 0.65)

    texts = [h.get("text", "") for h in inter]
    avg_len = sum(len(t) for t in texts) / max(1, n)
    questions = sum(1 for t in texts if "?" in t or "?" in t)
    courtesy = sum(1 for t in texts
                   if any(w in t for w in _COURTESY_WORDS) and len(t) <= 16)
    non_courtesy = 1 - courtesy / max(1, n)

    a_msgs = sum(1 for h in inter if h.get("who") == "A")
    b_msgs = n - a_msgs

    # 频率分:每月 1 条 = 20 分,封顶 40;证据上限 = 条数×10;再乘时间衰减
    f_score = min(40.0, freq * 20, n * 10) * decay
    # 深度分:平均长度 + 非纯客套占比 + 有问句,封顶 30(也乘轻度衰减)
    d_score = min(30.0, min(avg_len, 40) / 40 * 15 + non_courtesy * 10
                  + (5 if questions else 0)) * max(decay, 0.6)
    # 主动分:A 回复一次 +10(封顶 30);B 评论一次 +6(封顶 30)
    ab_active = min(30.0, a_msgs * 10) * decay
    ba_active = min(30.0, b_msgs * 6) * decay
    # 双向折扣:对方几乎不回应时,该方向好感打折(单向热情 → 0.3 折起)
    recip_ab = 0.3 + 0.7 * min(1.0, a_msgs / max(b_msgs, 1))
    recip_ba = 0.3 + 0.7 * min(1.0, b_msgs / max(a_msgs, 1))

    return {
        "count": n, "span_days": span_days,
        "window_days": window_days, "days_since_last": days_since_last,
        "recency_decay": round(decay, 2),
        "window_months": round(window_months, 1),
        "freq_per_month": round(freq, 2),
        "freq_score": round(f_score, 1), "depth_score": round(d_score, 1),
        "a_count": a_msgs, "b_count": b_msgs,
        "a_active_score": round(ab_active, 1), "b_active_score": round(ba_active, 1),
        "recip_ab": round(recip_ab, 2), "recip_ba": round(recip_ba, 2),
        "avg_len": round(avg_len, 1), "questions": questions,
        "courtesy_ratio": round(courtesy / max(1, n), 2),
    }


def _baseline(signals: dict, who: str) -> float:
    """One direction's baseline: (频率 + 深度 + 该方向主动分) × 双向折扣."""
    active = signals["a_active_score"] if who == "A" else signals["b_active_score"]
    recip = signals["recip_ab"] if who == "A" else signals["recip_ba"]
    return (signals["freq_score"] + signals["depth_score"] + active) * recip


_SUMMARY_SYSTEM_TEMPLATE = (
    "你是中文社交媒体关系分析师。互动记录的客观信号(频率/双向/深度)已经算出一个"
    "保守的好感度基准分,你的职责只是在基准上做小幅主观修正。只输出一个 JSON 对象:\n"
    '- "关系类型": 一句话,例如 亲密朋友 / 普通朋友 / 粉丝与博主 / 工作伙伴 / 逐渐疏远\n'
    '- "{an}对{bn}好感调整": -15 到 15 的整数,只修正基准没覆盖的主观因素'
    "(语气亲昵、玩笑默契、敷衍感、单方面热情等;互动很少或全是客套时倾向 0 或负数)\n"
    '- "{bn}对{an}好感调整": 同上\n'
    '- "好感度依据": 两句话内,说明为什么这样调整\n'
    '- "互动氛围": 一句话\n'
    '- "主要话题": 至多5个短字符串的数组\n'
    '- "分析": 120-200 字,行文直接用两人昵称,不要用 A/B 指代\n'
    '- "局限": 一句话说明数据局限\n'
    "硬性规则:客套互动(踩踩/谢谢/打卡)不能支撑高分;几个月才几条互动是弱关系信号;"
    "不要编造记录里没有的内容;调整永远不能超过 ±15。"
)


def summarize_relationship(bundle: dict, cfg: dict) -> dict:
    """AI summary of the A/B relationship: algorithmic baseline + bounded
    model adjustment (±15), so model generosity cannot inflate the score."""
    a_name = bundle.get("a_name") or bundle.get("a_uid") or "A"
    b_name = bundle.get("b_name") or bundle.get("b_uid") or "B"
    signals = relationship_signals(bundle)
    if not signals["count"]:
        raise JevError("没有互动记录,无法生成总结")
    base_ab, base_ba = _baseline(signals, "A"), _baseline(signals, "B")

    lines = []
    for h in bundle.get("interactions") or []:
        when = (h.get("ts") or "")[:16].replace("T", " ") or "时间未知"
        who = a_name if h.get("who") == "A" else b_name
        lines.append(f'{when} {who}({h.get("author") or "?"}): {(h.get("text") or "")[:80]}')

    user_prompt = (
        f"用户一(帖子作者): {a_name}({bundle.get('a_uid')})\n"
        f"用户二(评论者): {b_name}({bundle.get('b_uid')})\n"
        f"客观信号:互动 {signals['count']} 条 / 观察窗口 {signals['window_days']} 天"
        f"(月均 {signals['freq_per_month']} 条) · 距最近一次互动已 {signals['days_since_last']} 天"
        f"(时间衰减系数 {signals['recency_decay']}) · "
        f"{a_name} 发言 {signals['a_count']} 条 / {b_name} 发言 {signals['b_count']} 条 · "
        f"平均长度 {signals['avg_len']} 字 · 纯客套占比 {signals['courtesy_ratio']} · "
        f"问句 {signals['questions']} 个\n"
        f"好感度基准分:{a_name}对{b_name} = {base_ab:.0f}"
        f"(频率 {signals['freq_score']} + 深度 {signals['depth_score']} "
        f"+ {a_name}主动 {signals['a_active_score']});"
        f"{b_name}对{a_name} = {base_ba:.0f}"
        f"(频率 {signals['freq_score']} + 深度 {signals['depth_score']} "
        f"+ {b_name}主动 {signals['b_active_score']})\n"
        "以下是两人在帖子作者微博下评论区的互动记录,按时间排序(越靠下越新):\n"
        + "\n".join(lines)
    )
    drafter = ReplyDrafter(cfg["reply"]["base_url"], cfg["reply"]["model"],
                           cfg["reply"]["api_key"],
                           secrets=analyze.all_secrets(cfg))
    system = (_SUMMARY_SYSTEM_TEMPLATE
              .replace("{an}", a_name).replace("{bn}", b_name))
    content = drafter.chat(system, user_prompt)
    obj = _extract_json_object(content)

    def _adjust(key: str) -> int:
        try:
            return max(-15, min(15, int(obj.get(key))))
        except (TypeError, ValueError):
            return 0

    adj_ab = _adjust(f"{a_name}对{b_name}好感调整")
    adj_ba = _adjust(f"{b_name}对{a_name}好感调整")
    final_ab = int(max(5, min(95, base_ab + adj_ab)))
    final_ba = int(max(5, min(95, base_ba + adj_ba)))

    obj[f"{a_name}对{b_name}好感"] = final_ab
    obj[f"{b_name}对{a_name}好感"] = final_ba
    obj["好感度明细"] = {
        "互动数": signals["count"], "窗口天数": signals["window_days"],
        "距上次互动天数": signals["days_since_last"],
        "时间衰减": signals["recency_decay"],
        "月均条数": signals["freq_per_month"],
        "频率分": signals["freq_score"], "深度分": signals["depth_score"],
        f"{a_name}主动分": signals["a_active_score"],
        f"{b_name}主动分": signals["b_active_score"],
        f"{a_name}对{b_name}": {"基准": round(base_ab), "调整": adj_ab, "最终": final_ab},
        f"{b_name}对{a_name}": {"基准": round(base_ba), "调整": adj_ba, "最终": final_ba},
    }
    obj["_a_name"], obj["_b_name"] = a_name, b_name
    return obj


def _find_browser() -> str | None:
    """Prefer Chrome over Edge: on this machine Edge's headless mode fails when
    it can reuse a running session; Chrome with its own user-data-dir works."""
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _render_pdf(url: str, timeout: int = 90) -> bytes | None:
    """Render a URL to PDF via headless Chrome/Edge; None if unavailable.

    Each run gets its own --user-data-dir: reusing the default profile makes the
    browser delegate to an already-running session and never write the PDF.
    """
    exe = _find_browser()
    if not exe:
        return None
    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    udd = tempfile.mkdtemp(prefix="weibo_pdf_")
    try:
        for headless in ("--headless=new", "--headless"):
            cmd = [exe, headless, "--disable-gpu", "--no-first-run",
                   "--no-pdf-header-footer", "--print-to-pdf-no-header",
                   f"--user-data-dir={udd}", f"--print-to-pdf={path}", url]
            try:
                subprocess.run(cmd, capture_output=True, timeout=timeout)
            except (subprocess.TimeoutExpired, OSError):
                continue
            if os.path.getsize(path) > 500:
                with open(path, "rb") as f:
                    return f.read()
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
        try:
            import shutil
            shutil.rmtree(udd, ignore_errors=True)
        except OSError:
            pass

PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>微博分析</title>
<style>
  :root { --bg:#f4f5f7; --card:#fff; --line:#e3e6ea; --text:#1d2129;
          --muted:#86909c; --accent:#1f6feb; --warn:#c47f0a; --danger:#d03050; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text); font:15px/1.6 "Segoe UI","Microsoft YaHei",sans-serif;
         padding:24px 16px 60px; max-width:920px; margin:0 auto; }
  h1 { font-size:22px; margin-bottom:4px; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:16px; }
  .tabs { display:flex; gap:8px; margin-bottom:14px; }
  .tab { border:1px solid var(--line); background:#fff; border-radius:10px;
         padding:7px 18px; font-size:14px; cursor:pointer; color:var(--muted); }
  .tab.active { background:var(--accent); border-color:var(--accent); color:#fff; font-weight:600; }
  .form { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:16px 18px; display:flex; flex-direction:column; gap:12px; }
  .row { display:flex; gap:12px; flex-wrap:wrap; align-items:center; }
  label.mod { display:flex; gap:6px; align-items:center; cursor:pointer; font-size:14px; }
  input[type=text], textarea, select { border:1px solid var(--line); border-radius:8px;
          padding:8px 10px; font-size:14px; font-family:inherit; }
  textarea { width:100%; min-height:88px; resize:vertical; }
  .urlbox { flex:1; min-width:260px; }
  .field { display:flex; flex-direction:column; gap:4px; font-size:13px; color:var(--muted); }
  .small { width:180px; }
  button.primary { background:var(--accent); color:#fff; border:none; border-radius:8px;
          padding:10px 22px; font-size:15px; cursor:pointer; }
  button.primary:disabled { background:#9db8e2; cursor:wait; }
  .hint { color:var(--muted); font-size:12px; }
  #status, #istatus { min-height:22px; font-size:13px; color:var(--muted); }
  .error { background:#fdecea; border:1px solid #f5c2c2; color:var(--danger);
          border-radius:8px; padding:10px 14px; font-size:14px; white-space:pre-wrap; margin-top:10px; }
  .postbox { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:14px 16px; margin-top:14px; font-size:14px; }
  .postbox h3 { font-size:14px; margin-bottom:6px; }
  .postbox p { color:#4b5563; white-space:pre-wrap; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:14px 16px; margin-top:14px; }
  .card-h { font-size:14px; margin-bottom:6px; display:flex; justify-content:space-between; gap:8px; flex-wrap:wrap; }
  .card-h .muted { color:var(--muted); font-weight:normal; font-size:12px; }
  .qtext { background:#f8f9fb; border-radius:8px; padding:8px 10px; font-size:14px;
          margin:6px 0 10px; white-space:pre-wrap; }
  .ctx { color:var(--muted); font-size:12px; margin:0 0 10px; white-space:pre-wrap;
         border-left:2px solid var(--line); padding-left:8px; }
  .dims { display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:8px; margin-bottom:12px; }
  .dim { border:1px solid var(--line); border-radius:8px; padding:8px 10px; }
  .dim .t { font-size:12px; color:var(--muted); margin-bottom:2px; }
  .dim .v { font-size:13.5px; }
  .dim .v .warn { color:var(--warn); font-weight:600; }
  .dim .v .danger { color:var(--danger); font-weight:700; }
  .reps { border-top:1px dashed var(--line); padding-top:10px; }
  .reps h4 { font-size:13px; color:var(--muted); margin-bottom:8px; font-weight:600; }
  .rep { display:flex; gap:10px; align-items:baseline; padding:5px 0; font-size:14.5px; }
  .badge { background:#e8f1fd; color:var(--accent); border-radius:20px; min-width:52px;
          text-align:center; font-size:12.5px; padding:1px 8px; font-weight:600; }
  .rep.zero .badge { background:#f2f3f5; color:var(--muted); }
  .meta { color:var(--muted); font-size:12px; margin-top:8px; }
  .errline { color:var(--danger); font-size:13px; padding:4px 0; }
  .tl { margin-top:6px; }
  .tlhead { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:10px 14px; margin-top:14px; font-size:13.5px; color:#4b5563; font-weight:600; }
  .tlpost { background:var(--card); border:1px solid var(--line); border-radius:10px 10px 0 0;
          padding:8px 14px; font-size:13px; color:#4b5563; margin-top:14px; font-weight:600; }
  .tlrow { display:flex; gap:10px; align-items:baseline; background:var(--card);
          border:1px solid var(--line); border-top:none; padding:6px 14px; font-size:14px; }
  .tlrow:last-of-type { border-radius:0 0 10px 10px; }
  .when { color:var(--muted); font-size:12px; min-width:72px; }
  .who-badge { font-size:12.5px; padding:1px 9px; border-radius:8px; font-weight:600;
          max-width:150px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .ba { background:#e8f1fd; color:var(--accent); }
  .bb { background:#f2f3f5; color:var(--muted); }
  .author { color:#4b5563; font-size:13px; min-width:96px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:160px; }
  .txt { flex:1; }
  .userpick { display:flex; gap:8px; align-items:center; }
  .userpick input { flex:1; min-width:150px; }
  button.mini { background:#fff; border:1px solid var(--accent); color:var(--accent);
          border-radius:8px; padding:6px 14px; font-size:13px; cursor:pointer; }
  button.mini:disabled { border-color:var(--line); color:var(--muted); cursor:wait; }
  .picked { font-size:12.5px; color:var(--accent); background:#e8f1fd; border-radius:8px;
          padding:3px 10px; display:none; max-width:100%; overflow:hidden;
          text-overflow:ellipsis; white-space:nowrap; }
  .picked b { font-weight:600; }
  .candbox { position:relative; width:100%; }
  .cands { position:absolute; top:4px; left:0; right:0; z-index:30; background:#fff;
          border:1px solid var(--line); border-radius:10px; box-shadow:0 8px 24px rgba(0,0,0,.12);
          max-height:300px; overflow:auto; display:none; }
  .cands.open { display:block; }
  .cand { display:flex; gap:10px; align-items:center; padding:8px 10px; cursor:pointer;
          border-bottom:1px solid #f0f2f5; font-size:13.5px; }
  .cand:last-child { border-bottom:none; }
  .cand:hover { background:#f4f8ff; }
  .cand img { width:34px; height:34px; border-radius:50%; flex:none; }
  .cand .cn { font-weight:600; }
  .cand .cu { color:var(--muted); font-size:11.5px; }
  .cand .cd { color:var(--muted); font-size:11.5px; max-width:250px; overflow:hidden;
          text-overflow:ellipsis; white-space:nowrap; }
  .cand .vicon { color:#f6ca45; font-weight:700; }
  .cand.exact { background:#f0f7ff; }
  .cand.exact:hover { background:#e3effe; }
  .cand .tag { margin-left:auto; flex:none; font-size:11px; color:var(--accent);
          background:#e8f1fd; border-radius:6px; padding:1px 6px; }
  details.logs { margin-top:10px; font-size:12px; color:var(--muted); }
  .livelog { background:#f8f9fb; border:1px solid var(--line); border-radius:10px;
          padding:8px 12px; margin-top:12px; font:12px/1.55 Consolas,"Courier New",monospace;
          color:#4b5563; max-height:220px; overflow:auto; white-space:pre-wrap; }
  .livelog .done-line { color:var(--accent); font-weight:600; }
  .livelog .warn-line { color:var(--warn); }
  .sumcard .reltype { font-size:17px; font-weight:700; margin:2px 0 10px; }
  .sumcard .sbar { display:flex; align-items:center; gap:8px; margin:5px 0; }
  .sumcard .sl { width:120px; font-size:12.5px; color:var(--muted); }
  .sumcard .strack { flex:1; max-width:260px; height:12px; background:#f0f2f5;
          border-radius:6px; overflow:hidden; }
  .sumcard .strack span { display:block; height:100%; }
  .sumcard .sv { font-size:12.5px; color:#4b5563; }
  .sumcard .why { font-size:12.5px; color:var(--muted); margin:8px 0; }
  .sumcard .detail { font-size:12px; color:var(--muted); margin:4px 0 8px;
          line-height:1.7; font-family:Consolas,"Microsoft YaHei",monospace; }
  .sumcard .analysis { font-size:14px; margin:10px 0 6px; text-align:justify; }
  .sumcard .chip { display:inline-block; background:#eef4fe; color:var(--accent);
          border-radius:10px; padding:1px 9px; margin:2px 4px 2px 0; font-size:12px; }
  .sumcard .kv { font-size:13px; margin:8px 0; }
  .sumcard .kv th { text-align:left; color:var(--muted); font-weight:normal;
          padding-right:10px; vertical-align:top; }
  .exportbtn { background:#fff; border:1px solid var(--accent); color:var(--accent);
          border-radius:8px; padding:8px 18px; font-size:14px; cursor:pointer;
          margin-top:14px; }
  .exportbtn:hover { background:#f0f7ff; }
  details.logs pre { white-space:pre-wrap; background:#f8f9fb; border-radius:8px; padding:8px 10px; margin-top:6px; }
</style>
</head>
<body>
  <h1>微博分析</h1>
  <div class="sub">帖子 7 维判断与候选回复 · 评论区怎么回 · 两人互动时间线 · 数据只发给你配置的模型服务商</div>

  <div class="tabs">
    <button class="tab active" id="tab-post" onclick="showTab('post')">帖子 / 评论分析</button>
    <button class="tab" id="tab-inter" onclick="showTab('inter')">两人互动时间线</button>
  </div>

  <div id="pane-post">
    <div class="form">
      <div class="row">
        <label class="mod"><input type="radio" name="kind" value="url" checked> 帖子链接</label>
        <label class="mod"><input type="radio" name="kind" value="text"> 直接贴文本</label>
        <input type="text" id="urlinput" class="urlbox" placeholder="https://weibo.com/uid/AbCdEf... 或 m.weibo.cn/detail/... 或帖子 ID">
      </div>
      <div class="field" id="textfield" style="display:none">
        帖子正文
        <textarea id="textinput" placeholder="把微博正文粘贴到这里…"></textarea>
        <div class="row">
          <input type="text" id="authorinput" class="small" placeholder="作者昵称(可空)">
          <input type="text" id="contextinput" placeholder="额外上下文(可空,如原帖内容)">
        </div>
      </div>
      <div class="row">
        <label class="mod"><input type="checkbox" id="commentsbox"> 分析评论区</label>
        <select id="commentssel" disabled>
          <option value="all">全部(剔除楼主,含二级回复)</option>
          <option value="5">前 5 条</option>
          <option value="10">前 10 条</option>
          <option value="20">前 20 条</option>
        </select>
        <label class="mod"><input type="checkbox" id="posterbox"> 楼主口吻写回复</label>
        <select id="judgesel">
          <option value="auto">判断:自动(配置)</option>
          <option value="bocha">判断:博查 Jev</option>
          <option value="ds">判断:DeepSeek</option>
        </select>
        <button class="primary" id="gobtn">分 析</button>
      </div>
      <div class="hint">评论区模式需要帖子链接(自动抓取);每条评论 3 次模型调用、约 2 秒。</div>
    </div>
    <div id="status"></div>
    <div id="posts"></div>
    <div id="results"></div>
  </div>

  <div id="pane-inter" style="display:none">
    <div class="form">
      <div class="row">
        <div class="field">用户 A(帖子主人)
          <div class="candbox">
            <div class="userpick">
              <input type="text" id="userA" placeholder="昵称 / uid / u/ 链接,回车或点搜索">
              <button class="mini" id="searchA">搜索</button>
            </div>
            <div class="cands" id="candsA"></div>
          </div>
        </div>
        <div class="field">用户 B(互动对象)
          <div class="candbox">
            <div class="userpick">
              <input type="text" id="userB" placeholder="昵称 / uid / u/ 链接,回车或点搜索">
              <button class="mini" id="searchB">搜索</button>
            </div>
            <div class="cands" id="candsB"></div>
          </div>
        </div>
      </div>
      <div class="row">
        <span id="pickedA" class="picked"></span>
        <span id="pickedB" class="picked"></span>
      </div>
      <div class="row">
        <div class="field">扫描页数(每页约20帖)
          <select id="pagesSel">
            <option value="1">1 页</option>
            <option value="2">2 页</option>
            <option value="5" selected>5 页</option>
            <option value="10">10 页</option>
          </select>
        </div>
        <div class="field">最多输出
          <select id="limitSel">
            <option value="50">50 条</option>
            <option value="100" selected>100 条</option>
            <option value="200">200 条</option>
          </select>
        </div>
        <button class="primary" id="interbtn">生成时间线</button>
      </div>
      <div class="hint">填昵称后点「搜索」从候选里选择确认(推荐);直接生成也可以,会自动精确匹配并在日志里警告模糊项。扫描自动跳过置顶帖(其互动往往很早,会拉长观察窗口误判好感度);扫描需要已配置微博 Cookie。</div>
    </div>
    <div id="istatus"></div>
    <div id="ilivelog" class="livelog" style="display:none"></div>
    <div id="interout"></div>
  </div>

<script>
const $ = id => document.getElementById(id);
function showTab(name) {
  const post = name === 'post';
  $("pane-post").style.display = post ? "" : "none";
  $("pane-inter").style.display = post ? "none" : "";
  $("tab-post").classList.toggle("active", post);
  $("tab-inter").classList.toggle("active", !post);
}
$("gobtn").addEventListener("click", runPost);
for (const r of document.querySelectorAll("input[name=kind]")) {
  r.addEventListener("change", () => {
    const isText = document.querySelector("input[name=kind]:checked").value === "text";
    $("urlinput").style.display = isText ? "none" : "";
    $("textfield").style.display = isText ? "flex" : "none";
  });
}
$("commentsbox").addEventListener("change", () => { $("commentssel").disabled = !$("commentsbox").checked; });

async function runPost() {
  const kind = document.querySelector("input[name=kind]:checked").value;
  const payload = {
    kind: kind,
    input: kind === "url" ? $("urlinput").value.trim() : $("textinput").value.trim(),
    author: $("authorinput").value.trim(),
    context: $("contextinput").value.trim(),
    comments: $("commentsbox").checked ? $("commentssel").value : null,
    as_poster: $("posterbox").checked,
    judge: $("judgesel").value,
  };
  if (!payload.input) { $("status").textContent = "请先填写链接或正文"; return; }
  $("gobtn").disabled = true;
  $("status").textContent = "分析中…(评论区全量可能要几十秒到几分钟)";
  $("results").innerHTML = ""; $("posts").innerHTML = "";
  try {
    const resp = await fetch("/api/analyze", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    $("gobtn").disabled = false;
    if (!data.ok) {
      $("status").textContent = "";
      $("results").innerHTML = `<div class="error">${escapeHtml(data.error || "未知错误")}</div>`;
      return;
    }
    $("status").textContent = data.stats || "";
    $("posts").innerHTML = data.post_html || "";
    $("results").innerHTML = (data.cards || []).map(c => c.html).join("")
      + (data.errors && data.errors.length ? `<div class="error">${data.errors.map(escapeHtml).join("<br>")}</div>` : "");
  } catch (e) {
    $("gobtn").disabled = false;
    $("status").textContent = "";
    $("results").innerHTML = `<div class="error">请求失败:${escapeHtml(String(e))}</div>`;
  }
}

$("interbtn").addEventListener("click", runInter);
let pollTimer = null, lastJobId = null;
async function runInter() {
  const uidOf = side => picked[side] ? picked[side].uid : null;
  const nameOf = side => picked[side] ? (picked[side].screen_name || "") : "";
  const payload = {
    user_a: uidOf("A") || $("userA").value.trim(),
    user_b: uidOf("B") || $("userB").value.trim(),
    user_a_name: nameOf("A"), user_b_name: nameOf("B"),
    pages: parseInt($("pagesSel").value, 10), limit: parseInt($("limitSel").value, 10),
  };
  if (!payload.user_a || !payload.user_b) { $("istatus").textContent = "请填写用户 A 和 B"; return; }
  $("interbtn").disabled = true;
  $("istatus").textContent = "启动扫描…";
  $("interout").innerHTML = "";
  const live = $("ilivelog");
  live.style.display = "none"; live.textContent = "";
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  try {
    const resp = await fetch("/api/interactions", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!data.ok) {
      $("interbtn").disabled = false;
      $("istatus").textContent = "";
      $("interout").innerHTML = `<div class="error">${escapeHtml(data.error || "未知错误")}</div>`;
      return;
    }
    live.style.display = "";
    pollJob(data.job_id);
  } catch (e) {
    $("interbtn").disabled = false;
    $("istatus").textContent = "";
    $("interout").innerHTML = `<div class="error">请求失败:${escapeHtml(String(e))}</div>`;
  }
}
function renderLiveLog(lines, status) {
  const live = $("ilivelog");
  live.innerHTML = lines.map(l =>
    `<span class="${l.startsWith("[警告]") ? "warn-line" : ""}">${escapeHtml(l)}</span>`
  ).join("\n") + (status === "done" ? '\n<span class="done-line">—— 扫描完成 ——</span>' : "");
  live.scrollTop = live.scrollHeight;
}
function pollJob(jobId) {
  const started = Date.now();
  lastJobId = jobId;
  pollTimer = setInterval(async () => {
    try {
      const r = await fetch("/api/job?id=" + jobId);
      const d = await r.json();
      if (!d.ok) { finishPoll(d.error || "任务查询失败"); return; }
      const job = d.job;
      renderLiveLog(job.logs, job.status);
      const mins = ((Date.now() - started) / 60000).toFixed(1);
      $("istatus").textContent = job.status === "running"
        ? `扫描中… 已 ${mins} 分钟(最新:${(job.logs[job.logs.length - 1] || "").slice(0, 50)})`
        : "";
      if (job.status === "running") return;
      if (job.status === "error") { finishPoll(job.error || "扫描失败"); return; }
      finishPoll(null);
      const out = [];
      if (job.summary) out.push(summaryCardHtml(job));
      else if (job.summary_error) out.push(`<div class="error">AI 总结生成失败:${escapeHtml(job.summary_error)}</div>`);
      out.push(job.html || "");
      out.push(`<button class="exportbtn" id="exportbtn" onclick="exportPdf()">导出 PDF 报告</button>`);
      $("interout").innerHTML = out.join("");
    } catch (e) { /* transient poll error: keep polling */ }
  }, 2000);
}
function summaryCardHtml(job) {
  const s = job.summary;
  const an = (job.names && job.names[0]) || s["_a_name"] || "A";
  const bn = (job.names && job.names[1]) || s["_b_name"] || "B";
  const bar = (label, v) => {
    if (v === null || v === undefined) return `<div class="sbar"><span class="sl">${label}</span><span class="sv">无数据</span></div>`;
    const color = v >= 70 ? "#d03050" : (v >= 40 ? "#1f6feb" : "#86909c");
    return `<div class="sbar"><span class="sl">${label}</span><span class="strack"><span style="width:${v}%;background:${color}"></span></span><span class="sv">${v} / 100</span></div>`;
  };
  const detail = s["好感度明细"] || {};
  const ab = detail[`${an}对${bn}`] || {}, ba = detail[`${bn}对${an}`] || {};
  const part = d => d["基准"] === undefined ? "" :
    `基准 ${d["基准"]} · 模型调整 ${d["调整"] >= 0 ? "+" : ""}${d["调整"]} → 最终 ${d["最终"]}`;
  const detailLine = detail["互动数"] === undefined ? "" :
    `<p class="detail">互动 ${detail["互动数"]} 条 · 窗口 ${detail["窗口天数"]} 天(月均 ${detail["月均条数"]}) · 距上次互动 ${detail["距上次互动天数"]} 天 · 时间衰减 ×${detail["时间衰减"]} · 频率分 ${detail["频率分"]} · 深度分 ${detail["深度分"]}\n` +
    `${escapeHtml(an)}→${escapeHtml(bn)}:${escapeHtml(part(ab))}\n` +
    `${escapeHtml(bn)}→${escapeHtml(an)}:${escapeHtml(part(ba))}</p>`;
  const topics = Array.isArray(s["主要话题"]) ? s["主要话题"] : [];
  const chips = topics.slice(0, 5).map(t => `<span class="chip">${escapeHtml(String(t))}</span>`).join("");
  const ab_score = s[`${an}对${bn}好感`], ba_score = s[`${bn}对${an}好感`];
  return `<div class="card sumcard">
    <div class="card-h"><b>AI 关系总结</b><span class="muted">${escapeHtml(an)} × ${escapeHtml(bn)} · 模型生成,仅供参考</span></div>
    <div class="reltype">${escapeHtml(s["关系类型"] || "?")}</div>
    <div>${bar(escapeHtml(an) + " 对 " + escapeHtml(bn) + " 好感度", ab_score)}${bar(escapeHtml(bn) + " 对 " + escapeHtml(an) + " 好感度", ba_score)}</div>
    ${detailLine}
    <p class="why">${escapeHtml(s["好感度依据"] || "")}</p>
    <table class="kv"><tr><th>互动氛围</th><td>${escapeHtml(s["互动氛围"] || "")}</td></tr>
    <tr><th>主要话题</th><td>${chips}</td></tr></table>
    <p class="analysis">${escapeHtml(s["分析"] || "")}</p>
    <p class="muted">局限:${escapeHtml(s["局限"] || "")} · 好感度 = 客观基准(频率/双向/深度) + 模型微调(±15)</p>
  </div>`;
}
async function exportPdf() {
  if (!lastJobId) return;
  const btn = $("exportbtn");
  btn.disabled = true; btn.textContent = "正在生成 PDF…(约 5–10 秒)";
  try {
    // Probe once for errors; the actual download goes through an iframe so the
    // browser handles it natively (fetch+blob is unreliable for downloads).
    const probe = await fetch("/api/export/probe?id=" + lastJobId);
    const d = await probe.json();
    if (!d.ok) {
      if (d.fallback_url) { openPrintable(d.fallback_url, btn); return; }
      btn.textContent = "导出失败";
      alert(d.error || "导出失败");
      btn.disabled = false;
      return;
    }
    const frame = document.createElement("iframe");
    frame.style.display = "none";
    frame.src = "/api/export?id=" + lastJobId;
    document.body.appendChild(frame);
    setTimeout(() => frame.remove(), 60000);
    btn.textContent = "已开始下载 PDF ✓(若无下载请点此重试)";
    btn.disabled = false;
  } catch (e) {
    btn.textContent = "导出失败";
    btn.disabled = false;
    alert("导出失败: " + e);
  }
}
async function openPrintable(url, btn) {
  const w = window.open(url, "_blank");
  if (w) setTimeout(() => w.print(), 800);
  btn.textContent = "已打开打印页(选「另存为 PDF」)";
  btn.disabled = false;
}
function finishPoll(errMsg) {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  $("interbtn").disabled = false;
  if (errMsg) {
    $("istatus").textContent = "";
    $("interout").innerHTML = `<div class="error">${escapeHtml(errMsg)}</div>`;
  } else {
    $("istatus").textContent = "完成";
  }
}

// ---- user search & pick ----
const picked = {A: null, B: null};
function wireUser(side) {
  const input = $("user" + side), box = $("cands" + side), btn = $("search" + side);
  const doSearch = async () => {
    const q = input.value.trim();
    if (!q) { box.classList.remove("open"); return; }
    btn.disabled = true; btn.textContent = "搜索中";
    try {
      const resp = await fetch("/api/user-search", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({query: q}),
      });
      const data = await resp.json();
      btn.disabled = false; btn.textContent = "搜索";
      if (!data.ok) { box.classList.remove("open"); $("istatus").textContent = data.error || "搜索失败"; return; }
      $("istatus").textContent = "";
      renderCands(side, data.results, q);
    } catch (e) {
      btn.disabled = false; btn.textContent = "搜索";
      $("istatus").textContent = "搜索请求失败: " + e;
    }
  };
  btn.addEventListener("click", doSearch);
  input.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); doSearch(); } });
  input.addEventListener("input", () => { picked[side] = null; $("picked" + side).style.display = "none"; });
  document.addEventListener("click", e => {
    if (!box.contains(e.target) && e.target !== input && e.target !== btn) box.classList.remove("open");
  });
}
function renderCands(side, results, q) {
  const box = $("cands" + side);
  if (!results.length) { box.classList.remove("open"); $("istatus").textContent = "无搜索结果"; return; }
  box.innerHTML = results.map((r, i) => `
    <div class="cand${r.exact ? " exact" : ""}" data-i="${i}">
      ${r.avatar ? `<img src="${escapeHtml(r.avatar)}" referrerpolicy="no-referrer" onerror="this.style.visibility='hidden'">` : ""}
      <span><span class="cn">${escapeHtml(r.screen_name || "?")}${r.verified ? ' <span class="vicon">V</span>' : ""}</span>
      <span class="cu">uid ${escapeHtml(r.uid)}</span>${r.desc ? `<span class="cd">${escapeHtml(r.desc)}</span>` : ""}</span>
      ${r.exact ? '<span class="tag">精确匹配</span>' : ""}
    </div>`).join("");
  box.classList.add("open");
  for (const el of box.querySelectorAll(".cand")) {
    el.addEventListener("click", () => {
      const r = results[parseInt(el.dataset.i, 10)];
      picked[side] = r;
      $("user" + side).value = r.screen_name || r.uid;   // 框里显示昵称
      const tag = $("picked" + side);
      tag.innerHTML = `<b>${escapeHtml(r.screen_name || "")}</b> · ${escapeHtml(r.uid)} 已选定`;
      tag.style.display = "";
      box.classList.remove("open");
    });
  }
}
wireUser("A");
wireUser("B");

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
</script>
</body>
</html>
"""

PAGE_HTML = PAGE.encode("utf-8")


def esc(text) -> str:
    return html_lib.escape(str(text or ""))


def answer_html(qkey: str, answer: dict) -> str:
    if not answer:
        return "<span class='warn'>（无结果）</span>"
    from panel import _answer_to_text, _question_type, _score_index
    text = esc(_answer_to_text(qkey, answer))
    css = ""
    if _question_type(qkey) == "score":
        idx, _ = _score_index(qkey, answer)
        if qkey == "danger_level" and idx is not None:
            if idx >= 7:
                css = " class='danger'"
            elif idx >= 4:
                css = " class='warn'"
        if qkey == "aggression" and idx is not None and idx >= 5:
            css = " class='warn'"
    return f"<span{css}>{text}</span>"


def result_card(result: dict, as_poster: bool) -> str:
    from panel import QUESTION_TITLES
    post = result.get("post") or {}
    judge = result.get("judge") or {}
    answers = judge.get("answers") or {}
    replies = result.get("replies") or {}

    dims = "".join(
        f"<div class='dim'><div class='t'>{esc(QUESTION_TITLES[q])}</div>"
        f"<div class='v'>{answer_html(q, answers.get(q))}</div></div>"
        for q in QUESTION_TITLES if q in answers
    )
    rep_html = ""
    for i, item in enumerate(replies.get("ranked") or [], 1):
        prob = item.get("probability") or 0
        rep_html += (f"<div class='rep{'' if prob > 0.1 else ' zero'}'>"
                     f"<span class='badge'>{prob * 100:.0f}%</span>"
                     f"<span>{esc(item['text'])}</span></div>")
    if not rep_html and replies.get("candidates"):
        rep_html = "".join(f"<div class='rep'><span>{i}. {esc(t)}</span></div>"
                           for i, t in enumerate(replies["candidates"], 1))
    if replies.get("draft_error"):
        rep_html += f"<div class='errline'>草拟失败:{esc(replies['draft_error'])}</div>"
    if replies.get("rank_error"):
        rep_html += f"<div class='errline'>排序失败:{esc(replies['rank_error'])}</div>"

    backend = "聊天模型判断" if judge.get("backend") == "chat" else "Jev 判断"
    meta = (f"{esc(judge.get('model', '?'))} · {backend} · 判断 {judge.get('latency_ms', 0)}ms"
            f" · 总耗时 {result.get('total_latency_ms', 0)}ms")
    if result.get("tokens_in") or result.get("tokens_out"):
        meta += f" · token {result['tokens_in']}/{result['tokens_out']}"
    if result.get("cost") is not None:
        meta += f" · ${result['cost']:.5f}"

    kind = "评论" if post.get("kind") == "comment" else "帖子"
    tag = " · <span class='muted'>楼主口吻</span>" if as_poster and post.get("kind") == "comment" else ""
    ctx = (f"<div class='ctx'>{esc(post.get('context'))}</div>"
           if post.get("context") else "")
    return f"""<div class="card">
  <div class="card-h"><b>{esc(post.get('author') or '?')} · {esc(post.get('source') or '')}{tag}</b>
    <span class="muted">{kind}</span></div>
  <div class="qtext">{esc(post.get('text', ''))[:800]}</div>{ctx}
  <div class="dims">{dims}</div>
  <div class="reps"><h4>候选回复(按最合适排序)</h4>{rep_html}</div>
  <div class="meta">{meta}</div>
</div>"""


def timeline_html(bundle: dict) -> str:
    out = []
    a_name = bundle.get("a_name") or bundle.get("a_uid") or "A"
    b_name = bundle.get("b_name") or bundle.get("b_uid") or "B"
    last_post = None
    for h in bundle.get("interactions") or []:
        if h["post_id"] != last_post:
            summary = esc((h.get("post_summary") or "").replace("\n", " "))
            out.append(f"<div class='tlpost'>帖 {esc(h['post_id'])} · {summary[:60]}</div>")
            last_post = h["post_id"]
        try:
            when = (datetime.fromisoformat(h["ts"])
                    .astimezone(interactions._CN_TZ).strftime("%m-%d %H:%M"))
        except (TypeError, ValueError):
            when = "时间未知"
        is_a = h["who"] == "A"
        cls = "ba" if is_a else "bb"
        who_shown = a_name if is_a else b_name
        out.append(
            f"<div class='tlrow'><span class='when'>{when}</span>"
            f"<span class='who-badge {cls}'>{esc(who_shown)}</span>"
            f"<span class='txt'>{esc(h['text'])[:200]}</span></div>")
    stats = (f"{esc(a_name)} 与 {esc(b_name)} · "
             f"扫描 {bundle.get('posts_scanned')} 帖 · 命中 {bundle.get('total_hits')} 条 · "
             f"输出最近 {len(bundle.get('interactions') or [])} 条")
    return (f"<div class='tlhead'>{esc(stats)}</div><div class='tl'>"
            + "".join(out) + "</div>")


def api_analyze(data: dict) -> dict:
    cfg = analyze.load_config(analyze.CONFIG_PATH)
    try:
        judge = analyze.make_judge(cfg, force=data.get("judge") or "auto")
    except JevError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        kind = data.get("kind") or "url"
        if kind == "url":
            bid = extract_bid(data.get("input") or "")
            page = fetch_post(bid, cfg.get("weibo_cookie", ""))
            post = {"author": page.author, "text": page.text,
                    "retweeted_text": page.retweeted_text,
                    "source": page.source, "bid": page.bid}
        else:
            post = {"author": (data.get("author") or "").strip(),
                    "text": (data.get("input") or "").strip(),
                    "retweeted_text": "", "source": "手动输入"}
            page = None
        post["context"] = (data.get("context") or "").strip()
    except WeiboError as exc:
        return {"ok": False, "error": str(exc)}
    if not post["text"]:
        return {"ok": False, "error": "帖子内容为空"}

    as_poster = bool(data.get("as_poster"))
    perspective = ("你是这条评论所在原帖的作者，现在正以原作者的视角和口吻"
                   "回复这条评论，可以直接提到自己原帖里的内容。") if as_poster else ""

    post_html = (f"<div class='postbox'><h3>帖子 · {esc(post['author'] or '?')} · "
                 f"{esc(post['source'])}</h3><p>{esc(post['text'])[:500]}</p></div>")

    comments_req = data.get("comments")
    if comments_req is not None:
        if page is None:
            return {"ok": False, "error": "评论区模式需要帖子链接"}
        try:
            comments = fetch_comments(page.bid, cfg.get("weibo_cookie", ""),
                                      max_comments=300)
        except WeiboError as exc:
            return {"ok": False, "error": str(exc)}
        poster_id = (page.author_id or "").strip()
        others = [c for c in comments
                  if not poster_id or str(c.get("author_id", "")) != str(poster_id)]
        if str(comments_req).lower() in ("all", "全部"):
            selected = others
        else:
            try:
                selected = others[:max(1, int(comments_req))]
            except (TypeError, ValueError):
                selected = others
        mine = len(comments) - len(others)
        if not selected:
            return {"ok": True, "stats": "除楼主外无其它评论",
                    "post_html": post_html, "cards": [], "errors": []}

        parent_map = {c["idstr"]: c for c in comments}
        context_base = (f"原帖(作者 {post['author'] or '?'}): "
                        f"{analyze._truncate(post['text'], 300)}")
        cards, errors = [], []
        for c in selected:
            parent = parent_map.get(c.get("parent") or "")
            level_tag = "一级评论" if c["level"] == 1 else "二级回复"
            relation = f"这条{level_tag}回复的是微博帖子。{context_base}"
            if parent:
                relation += (f" 它直接回复的一级评论是(作者 {parent.get('author', '?')}):"
                             f"{analyze._truncate(parent.get('text', ''), 200)}")
            cpost = {"author": c["author"], "text": c["text"], "retweeted_text": "",
                     "context": relation,
                     "source": f"{level_tag} {c['idstr']} @ 帖子 {page.bid}",
                     "kind": "comment"}
            try:
                result, code = analyze.analyze_one(cpost, cfg, judge,
                                                   perspective=perspective)
                cards.append({"html": result_card(result, as_poster)})
                if code != 0:
                    errors.append(f"分析失败:{c['author'] or '?'}")
            except JevError as exc:
                errors.append(f"分析失败:{c['author'] or '?'}: {exc}")
        stats = (f"共 {len(comments)} 条评论(一级 {sum(1 for c in comments if c['level'] == 1)}"
                 f" + 二级 {sum(1 for c in comments if c['level'] == 2)}),"
                 f"剔除楼主 {mine} 条,已分析 {len(cards)} 条")
        return {"ok": True, "stats": stats, "post_html": post_html,
                "cards": cards, "errors": errors}

    result, code = analyze.analyze_one(post, cfg, judge, perspective=perspective)
    if code != 0:
        return {"ok": False, "error": "分析失败,详见后端控制台输出"}
    return {"ok": True, "stats": "分析完成", "post_html": "",
            "cards": [{"html": result_card(result, as_poster)}], "errors": []}


def api_interactions(data: dict) -> dict:
    """Start a background scan job; the frontend polls GET /api/job for logs."""
    cfg = analyze.load_config(analyze.CONFIG_PATH)
    cookie = (cfg.get("weibo_cookie") or "").strip()
    user_a = (data.get("user_a") or "").strip()
    user_b = (data.get("user_b") or "").strip()
    name_a = (data.get("user_a_name") or "").strip()
    name_b = (data.get("user_b_name") or "").strip()
    if not user_a or not user_b:
        return {"ok": False, "error": "请填写用户 A 和 B"}
    try:
        pages = max(1, min(int(data.get("pages") or 5), 20))
        limit = max(1, min(int(data.get("limit") or 100), 500))
    except (TypeError, ValueError):
        return {"ok": False, "error": "pages/limit 参数不合法"}

    try:
        ra = interactions.resolve_uid(user_a, cookie)
        rb = interactions.resolve_uid(user_b, cookie)
    except WeiboError as exc:
        return {"ok": False, "error": str(exc)}
    if ra["uid"] == rb["uid"]:
        return {"ok": False, "error": "A 和 B 是同一个用户"}

    logs: list[str] = []
    for tag, r, given in (("A", ra, name_a), ("B", rb, name_b)):
        shown = given or r["screen_name"] or "(uid)"
        if r["exact"] or given:
            logs.append(f"[用户] {tag}={shown}({r['uid']})")
        else:
            logs.append(f"[警告] {tag} 没有精确匹配的昵称,取搜索首条 {shown}({r['uid']});"
                        f"如不对请改用 uid / /u/ 链接")

    import uuid
    job_id = uuid.uuid4().hex[:12]
    job = {"logs": list(logs), "status": "running", "error": None,
           "html": "", "stats": "", "bundle": None, "summary": None,
           "summary_error": None, "finished_at": "", "names": (None, None)}
    with _JOBS_LOCK:
        _prune_jobs()
        _JOBS[job_id] = job

    def worker() -> None:
        progress = _job_progress(job)
        try:
            bundle = interactions.collect_interactions(
                ra["uid"], rb["uid"], cookie, pages, limit, progress=progress)
            bundle["a_name"] = name_a or ra["screen_name"] or ""
            bundle["b_name"] = name_b or rb["screen_name"] or ""
            for h in bundle.get("interactions") or []:
                h.pop("_dt", None)
            summary, summary_error = None, None
            try:
                progress("[总结] 正在生成 AI 关系总结…")
                summary = summarize_relationship(bundle, cfg)
            except (JevError, ValueError) as exc:
                summary_error = str(exc)
            with _JOBS_LOCK:
                job["bundle"] = bundle
                job["summary"] = summary
                job["summary_error"] = summary_error
                job["names"] = (bundle["a_name"], bundle["b_name"])
                job["html"] = timeline_html(bundle)
                job["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                job["status"] = "done"
        except (WeiboError, JevError) as exc:
            with _JOBS_LOCK:
                job["status"] = "error"
                job["error"] = str(exc)
        except Exception as exc:
            with _JOBS_LOCK:
                job["status"] = "error"
                job["error"] = f"{type(exc).__name__}: {exc}"

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "job_id": job_id}


def api_job(job_id: str) -> dict:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return {"ok": False, "error": "任务不存在或已过期"}
        return {"ok": True, "job": {"status": job["status"], "error": job["error"],
                                     "logs": list(job["logs"]), "html": job["html"],
                                     "stats": job["stats"],
                                     "summary": job["summary"],
                                     "summary_error": job["summary_error"],
                                     "finished_at": job["finished_at"],
                                     "names": list(job["names"])}}


def api_user_search(data: dict) -> dict:
    cfg = analyze.load_config(analyze.CONFIG_PATH)
    cookie = (cfg.get("weibo_cookie") or "").strip()
    query = (data.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "请输入要搜索的昵称或 uid"}
    results = interactions.search_user_api(query, cookie)
    return {"ok": True, "results": results[:20]}


def _bar(label: str, value) -> str:
    if value is None:
        return f"<div class='sbar'><span class='sl'>{esc(label)}</span><span class='sv'>无数据</span></div>"
    pct = max(0, min(100, int(value)))
    color = "#d03050" if pct >= 70 else ("#1f6feb" if pct >= 40 else "#86909c")
    return (f"<div class='sbar'><span class='sl'>{esc(label)}</span>"
            f"<span class='strack'><span style='width:{pct}%;background:{color}'></span></span>"
            f"<span class='sv'>{pct} / 100</span></div>")


def _detail_line(summary: dict, a_name: str, b_name: str) -> str:
    detail = summary.get("好感度明细") or {}
    ab = detail.get(f"{a_name}对{b_name}") or {}
    ba = detail.get(f"{b_name}对{a_name}") or {}

    def part(d: dict) -> str:
        if not d:
            return ""
        adj = d.get("调整", 0)
        return (f"基准 {d.get('基准', '?')} · 模型调整 {adj:+d} → 最终 {d.get('最终', '?')}")
    sig = (f"互动 {detail.get('互动数', '?')} 条 · 窗口 {detail.get('窗口天数', '?')} 天"
           f"(月均 {detail.get('月均条数', '?')}) · 距上次互动 {detail.get('距上次互动天数', '?')} 天"
           f" · 时间衰减 ×{detail.get('时间衰减', '?')}")
    return (f"<p class='detail'>{esc(sig)}<br>"
            f"{esc(a_name)}→{esc(b_name)}:{esc(part(ab))}<br>"
            f"{esc(b_name)}→{esc(a_name)}:{esc(part(ba))}</p>")


def summary_section(summary: dict | None, summary_error: str | None,
                    a_name: str, b_name: str) -> str:
    if summary is None:
        note = esc(summary_error or "未生成")
        return (f"<section><h2>AI 关系总结</h2>"
                f"<p class='muted'>总结不可用:{note}</p></section>")
    topics = summary.get("主要话题") or []
    if isinstance(topics, list):
        chips = "".join(f"<span class='chip'>{esc(str(t))}</span>" for t in topics[:5])
    else:
        chips = esc(str(topics))
    a_bar = summary.get(f"{a_name}对{b_name}好感")
    b_bar = summary.get(f"{b_name}对{a_name}好感")
    return f"""<section>
  <h2>AI 关系总结</h2>
  <div class="reltype">{esc(summary.get('关系类型') or '?')}</div>
  <div class="sbars">{_bar(f'{esc(a_name)} 对 {esc(b_name)} 好感度', a_bar)}
    {_bar(f'{esc(b_name)} 对 {esc(a_name)} 好感度', b_bar)}</div>
  {_detail_line(summary, a_name, b_name)}
  <p class="why">{esc(summary.get('好感度依据') or '')}</p>
  <table class="kv">
    <tr><th>互动氛围</th><td>{esc(summary.get('互动氛围') or '')}</td></tr>
    <tr><th>主要话题</th><td>{chips}</td></tr>
  </table>
  <p class="analysis">{esc(summary.get('分析') or '')}</p>
  <p class="muted">局限:{esc(summary.get('局限') or '')} · 好感度为「客观基准 + 模型微调(±15)」的启发式估算</p>
</section>"""


def report_html(job: dict) -> str:
    bundle = job.get("bundle") or {}
    a_name, b_name = job.get("names") or (None, None)
    a_uid, b_uid = bundle.get("a_uid", "?"), bundle.get("b_uid", "?")
    a_shown = a_name or a_uid
    b_shown = b_name or b_uid
    inter = bundle.get("interactions") or []

    rows: list[str] = []
    last_post = None
    for h in inter:
        if h.get("post_id") != last_post:
            summary_txt = esc((h.get("post_summary") or "").replace("\n", " "))
            rows.append(f"<tr class='postrow'><td colspan='3'>帖子 {esc(h.get('post_id'))}"
                        f" · {summary_txt[:60]}</td></tr>")
            last_post = h.get("post_id")
        try:
            when = datetime.fromisoformat(h["ts"]).astimezone(
                interactions._CN_TZ).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            when = "时间未知"
        is_a = h.get("who") == "A"
        cls = "a" if is_a else "b"
        who_shown = a_shown if is_a else b_shown
        rows.append(
            f"<tr><td class='when'>{when}</td>"
            f"<td class='who {cls}'>{esc(who_shown)}</td>"
            f"<td>{esc(h.get('text') or '')[:300]}</td></tr>")

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>微博互动分析报告 · {esc(a_shown)} × {esc(b_shown)}</title>
<style>
  @page {{ size: A4; margin: 14mm 12mm; }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font:12.5px/1.65 "Segoe UI","Microsoft YaHei",sans-serif; color:#1d2129; }}
  h1 {{ font-size:20px; margin-bottom:2px; }}
  h2 {{ font-size:15px; margin:18px 0 8px; padding-bottom:4px; border-bottom:2px solid #1f6feb; }}
  .stamp {{ color:#86909c; font-size:11.5px; margin-bottom:10px; }}
  .users {{ display:flex; gap:10px; margin:10px 0; }}
  .ucard {{ flex:1; border:1px solid #e3e6ea; border-radius:8px; padding:8px 12px; }}
  .ucard b {{ font-size:14px; }}
  .ucard .uid {{ color:#86909c; font-size:11px; }}
  .statline {{ color:#4b5563; font-size:12px; margin:6px 0 2px; }}
  .muted {{ color:#86909c; font-size:11px; }}
  .reltype {{ font-size:16px; font-weight:700; margin:6px 0 10px; }}
  .sbar {{ display:flex; align-items:center; gap:8px; margin:4px 0; }}
  .sl {{ width:150px; font-size:12px; }}
  .strack {{ flex:1; height:12px; background:#f0f2f5; border-radius:6px; overflow:hidden; }}
  .strack span {{ display:block; height:100%; }}
  .sv {{ font-size:12px; color:#4b5563; }}
  .sbars {{ margin:6px 0; }}
  .why {{ font-size:12px; color:#4b5563; margin:4px 0 8px; }}
  .detail {{ font-size:11px; color:#86909c; margin:2px 0 8px; line-height:1.6; }}
  .kv {{ border-collapse:collapse; margin:6px 0; width:100%; }}
  .kv th {{ text-align:left; font-size:11.5px; color:#86909c; width:80px;
            border-bottom:1px dashed #e3e6ea; padding:3px 6px 3px 0; font-weight:normal; vertical-align:top; }}
  .kv td {{ border-bottom:1px dashed #e3e6ea; padding:3px 0; font-size:12px; }}
  .chip {{ display:inline-block; background:#eef4fe; color:#1f6feb; border-radius:10px;
          padding:1px 8px; margin:1px 3px 1px 0; font-size:11px; }}
  .analysis {{ font-size:12.5px; margin:8px 0 4px; text-align:justify; }}
  table.tl {{ border-collapse:collapse; width:100%; margin-top:4px; }}
  table.tl td {{ border-bottom:1px solid #f0f2f5; padding:4px 6px 4px 0;
                 vertical-align:top; page-break-inside:avoid; }}
  td.when {{ color:#86909c; font-size:11px; white-space:nowrap; width:100px; }}
  td.who {{ font-size:11.5px; width:150px; max-width:170px; overflow:hidden;
            text-overflow:ellipsis; white-space:nowrap; }}
  td.who.a {{ color:#1f6feb; font-weight:600; }}
  td.who.b {{ color:#4b5563; font-weight:600; }}
  tr.postrow td {{ background:#f8f9fb; font-size:11.5px; font-weight:600; color:#4b5563;
                   padding:6px 6px 6px 0; page-break-inside:avoid; }}
  footer {{ margin-top:16px; color:#86909c; font-size:10.5px; border-top:1px solid #e3e6ea; padding-top:6px; }}
</style></head><body>
<h1>微博互动分析报告</h1>
<div class="stamp">生成时间:{esc(job.get('finished_at') or datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</div>
<div class="users">
  <div class="ucard"><b>{esc(a_shown)}</b><br><span class="uid">帖子作者 · uid {esc(a_uid)}</span></div>
  <div class="ucard"><b>{esc(b_shown)}</b><br><span class="uid">互动对象 · uid {esc(b_uid)}</span></div>
</div>
<div class="statline">扫描 {bundle.get('posts_scanned', 0)} 个帖子 · 命中 {bundle.get('total_hits', 0)} 条互动 ·
  本报告收录最近 {len(inter)} 条({esc(a_shown)} 的帖子下,{esc(b_shown)} 的评论与 {esc(a_shown)} 在相关楼里的回复)</div>
{summary_section(job.get("summary"), job.get("summary_error"), a_shown, b_shown)}
<section>
  <h2>互动时间线</h2>
  <table class="tl">{''.join(rows) or '<tr><td colspan="3" class="muted">无互动记录</td></tr>'}</table>
</section>
<footer>由本地工具「微博分析」生成 · 数据来自微博公开接口与本机已配置的模型服务 · AI 总结仅供参考,不代表真实关系</footer>
</body></html>"""


def api_export(job_id: str):
    """Return (pdf_bytes, filename) or an error/fallback dict."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        return {"ok": False, "error": "任务不存在或已过期(服务重启后任务会清空,请重新生成时间线)"}
    if job["status"] != "done":
        return {"ok": False, "error": "任务尚未完成,无法导出"}
    url = f"http://127.0.0.1:{SERVER_PORT}/report/{job_id}"
    pdf = _render_pdf(url)
    if pdf:
        a_name, b_name = job.get("names") or (None, None)
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        base = f"微博互动报告_{a_name or bundle_uid(job, 'a')}_{b_name or bundle_uid(job, 'b')}_{stamp}"
        from urllib.parse import quote
        return (pdf, f"weibo_report_{stamp}_{job_id}.pdf",
                quote(f"{base}.pdf"))
    return {"ok": False, "error": "未找到可用的 Chrome/Edge,无法直接生成 PDF;"
            "已改为打开可打印的报告页", "fallback_url": url}


def api_export_probe(job_id: str) -> dict:
    """Lightweight pre-check for the frontend: job exists? browser available?"""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        return {"ok": False, "error": "任务不存在或已过期(服务重启后任务会清空,请重新生成时间线)"}
    if job["status"] != "done":
        return {"ok": False, "error": "任务尚未完成,无法导出"}
    if not _find_browser():
        return {"ok": False, "fallback_url": f"/report/{job_id}",
                "error": "未找到可用的 Chrome/Edge,将打开可打印的报告页"}
    return {"ok": True}


def bundle_uid(job: dict, side: str) -> str:
    bundle = job.get("bundle") or {}
    return bundle.get(f"{side}_uid") or "?"


class Handler(BaseHTTPRequestHandler):
    server_version = "WeiboUI/1.0"
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, PAGE_HTML, "text/html; charset=utf-8")
        elif parsed.path == "/api/job":
            from urllib.parse import parse_qs
            job_id = (parse_qs(parsed.query).get("id") or [""])[0]
            self._json(200, api_job(job_id))
        elif parsed.path == "/api/export":
            # GET too, so an address-bar visit or link click also downloads
            from urllib.parse import parse_qs
            job_id = (parse_qs(parsed.query).get("id") or [""])[0]
            self._dispatch_export(job_id)
        elif parsed.path == "/api/export/probe":
            from urllib.parse import parse_qs
            job_id = (parse_qs(parsed.query).get("id") or [""])[0]
            self._json(200, api_export_probe(job_id))
        elif parsed.path.startswith("/report/"):
            job_id = parsed.path[len("/report/"):].strip("/")
            with _JOBS_LOCK:
                job = _JOBS.get(job_id)
            if job is None or job["status"] != "done":
                self._send(404, "任务不存在或未完成".encode("utf-8"),
                           "text/plain; charset=utf-8")
            else:
                self._send(200, report_html(job).encode("utf-8"),
                           "text/html; charset=utf-8")
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def _dispatch_export(self, job_id: str) -> None:
        """Serve the export result: PDF bytes or JSON fallback/error."""
        result = api_export(job_id)
        if isinstance(result, tuple):
            pdf, ascii_name, utf8_name = result
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header(
                "Content-Disposition",
                f"attachment; filename=\"{ascii_name}\"; "
                f"filename*=UTF-8''{utf8_name}")
            self.send_header("Content-Length", str(len(pdf)))
            self.end_headers()
            self.wfile.write(pdf)
        else:
            status = 200
            if "任务不存在" in (result.get("error") or ""):
                status = 404
            elif result.get("error") and not result.get("fallback_url"):
                status = 400
            self._json(status, result)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"ok": False, "error": "请求体不是合法 JSON"})
            return
        path = urlparse(self.path).path
        try:
            if path == "/api/analyze":
                self._json(200, api_analyze(data))
            elif path == "/api/interactions":
                self._json(200, api_interactions(data))
            elif path == "/api/user-search":
                self._json(200, api_user_search(data))
            elif path == "/api/export":
                from urllib.parse import parse_qs
                job_id = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
                self._dispatch_export(job_id)
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except (WeiboError, JevError) as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # any other failure surfaces as JSON, not a dead tab
            self._json(500, {"ok": False,
                             "error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, fmt, *args) -> None:
        sys.stderr.write("[ui] " + (fmt % args) + "\n")


def main() -> int:
    global SERVER_PORT
    parser = argparse.ArgumentParser(description="微博分析 - 本地图形界面(标准库实现)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开浏览器")
    args = parser.parse_args()
    SERVER_PORT = args.port

    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(
            f"http://{args.host}:{args.port}/")).start()
    print(f"微博分析界面已启动: http://{args.host}:{args.port}/ (Ctrl+C 退出)", flush=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())