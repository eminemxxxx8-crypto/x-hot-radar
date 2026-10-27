#!/usr/bin/env python3
"""Zero-paid-API celebrity news radar: public Google Trends + Google News RSS -> Telegram.

Signals are NOT direct X engagement analytics. State is saved to data/state.json and
committed by GitHub Actions only when something changes. All secrets via environment.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
import unicodedata
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

LOG = logging.getLogger("hotradar")
UTC = dt.timezone.utc
STATE_PATH = Path("data/state.json")
MAX_AGE_MINUTES = 105
MAX_ALERTS_PER_RUN = 2
COOLDOWN_HOURS = 8
HTNS = "https://trends.google.com/trending/rss"

# Famous people, not a closed allowlist: high-profile events also match broad terms.
ALIASES = {
    "姆巴佩": ["mbappe", "mbappé", "姆巴佩"],
    "梅西": ["messi", "lionel messi", "梅西"],
    "C罗": ["cristiano ronaldo", "ronaldo", "c罗", "c羅", "罗纳尔多"],
    "凯恩": ["harry kane", "凯恩"],
    "亚马尔": ["lamine yamal", "亚马尔"],
    "内马尔": ["neymar", "内马尔"],
    "哈兰德": ["haaland", "哈兰德"],
    "詹姆斯": ["lebron james", "勒布朗", "詹姆斯"],
    "库里": ["stephen curry", "steph curry", "斯蒂芬库里", "库里"],
    "杜兰特": ["kevin durant", "杜兰特"],
    "东契奇": ["luka doncic", "东契奇"],
    "马斯克": ["elon musk", "马斯克"],
    "比伯": ["justin bieber", "比伯"],
    "泰勒斯威夫特": ["taylor swift", "霉霉", "泰勒斯威夫特"],
    "蕾哈娜": ["rihanna", "蕾哈娜"],
    "坎耶": ["kanye", "ye west", "坎耶"],
    "碧昂丝": ["beyonce", "beyoncé", "碧昂丝"],
    "德雷克": ["drake", "德雷克"],
    "赛琳娜": ["selena gomez", "赛琳娜"],
    "金卡戴珊": ["kim kardashian", "卡戴珊"],
    "汤姆克鲁斯": ["tom cruise", "汤姆克鲁斯"],
    "迪卡普里奥": ["leonardo dicaprio", "莱昂纳多"],
    "道恩强森": ["dwayne johnson", "the rock", "道恩强森"],
    "大谷翔平": ["shohei ohtani", "大谷翔平"],
    "凯特琳克拉克": ["caitlin clark", "凯特琳克拉克"],
}

ALERT_WORDS = (
    "apolog", "controvers", "sponsor", "contract", "backlash", "boycott",
    "lawsuit", "sued", "arrest", "charged", "ban ", "suspend", "scandal",
    "breakup", "split", "cancel", "viral", "outrag", "statement", "fired",
    "ballon d'or", "ballon dor", "golden ball", "awards", "award", "record",
    "remark", "interview", "deleted", "attack", "conflict", "resign", "engaged",
    "道歉", "争议", "解约", "终止合作", "代言", "回应", "热搜", "爆料", "发言",
    "停播", "封禁", "被捕", "起诉", "金球奖", "破纪录", "分手", "退役", "丑闻", "取消",
    "获奖", "夺冠", "质疑", "声明", "封杀", "互撕", "表情包", "视频",
)

# Routine scores and match previews are not what this audience wants.
BORING_WORDS = (
    "match preview", "predicted lineup", "starting xi", "training session",
    "how to watch", "kick off", "kickoff", "live stream", "injury update",
    "fantasy football", "odds", "betting", "lineups", "transfer rumours",
    "赛前", "首发名单", "比分直播", "盘口", "赔率", "伤病报告",
)

# Google News publicly available RSS search. Limit the number of queries to be gentle.
NEWS_QUERIES = [
    '(("Mbappe" OR "Messi" OR "Ronaldo" OR "Harry Kane" OR "Yamal" OR "Neymar") (controversy OR apology OR sponsor OR scandal OR award OR viral)) when:4h',
    '(("Justin Bieber" OR "Taylor Swift" OR "Drake" OR "Kanye" OR "Selena Gomez" OR "Kim Kardashian") (controversy OR breakup OR apology OR viral OR lawsuit OR statement)) when:4h',
    '(("LeBron James" OR "Stephen Curry" OR "Elon Musk" OR "Shohei Ohtani") (controversy OR viral OR lawsuit OR apology OR statement)) when:4h',
    '(("姆巴佩" OR "梅西" OR "C罗" OR "马斯克" OR "比伯" OR "亚马尔") (道歉 OR 争议 OR 解约 OR 代言 OR 金球奖 OR 回应)) when:4h',
]


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def normalized(text: str) -> str:
    s = unicodedata.normalize("NFKD", html.unescape(text or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def contains_alias(text: str, alias: str) -> bool:
    hay = normalized(text)
    needle = normalized(alias)
    # For short roman names, avoid matching part of another word.
    if needle.isascii() and all(ch.isalnum() or ch == " " for ch in needle):
        return re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", hay) is not None
    return needle in hay


def get_entity(text: str) -> str | None:
    for display, aliases in ALIASES.items():
        if any(contains_alias(text, alias) for alias in aliases):
            return display
    return None


def event_type(text: str) -> str:
    t = normalized(text)
    if any(w in t for w in ("sponsor", "contract", "解约", "代言", "终止合作")):
        return "品牌合作"
    if any(w in t for w in ("apolog", "道歉")):
        return "道歉回应"
    if any(w in t for w in ("ballon", "金球奖", "award", "获奖")):
        return "奖项"
    if any(w in t for w in ("arrest", "charged", "lawsuit", "sued", "被捕", "起诉")):
        return "法律事件"
    if any(w in t for w in ("breakup", "split", "分手")):
        return "感情事件"
    return "舆论争议"


def score_item(item: dict) -> tuple[int, str | None]:
    title = item["title"]
    t = normalized(title)
    entity = get_entity(title)
    if not entity:
        return 0, None
    if any(term in t for term in BORING_WORDS):
        return 0, None
    is_hot = any(term in t for term in ALERT_WORDS)
    if not is_hot:
        # A rising name without known event context is too noisy.
        return 0, None
    score = 4
    if item["kind"] == "trend":
        score += 2
        if item.get("traffic", 0) >= 10000:
            score += 2
    if any(w in t for w in ("apolog", "解约", "controvers", "道歉", "boycott", "sponsor", "scandal")):
        score += 2
    return score, entity


def parse_when(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        val = email.utils.parsedate_to_datetime(value)
        return val.replace(tzinfo=UTC) if val.tzinfo is None else val.astimezone(UTC)
    except (ValueError, TypeError, IndexError):
        return None


def http_get(url: str, timeout: float = 12) -> bytes:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 HotRadar/1.0 (RSS feed reader)", "Accept": "application/rss+xml, text/xml, */*"})
    last_error = None
    for attempt in range(2):
        try:
            with urlopen(req, timeout=timeout) as response:
                data = response.read(2_000_000)
                if not data:
                    raise ValueError("empty response")
                return data
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(1)
    raise RuntimeError(f"RSS request failed: {type(last_error).__name__}") from last_error


def parse_trends(xml: bytes, geo: str) -> list[dict]:
    root = ET.fromstring(xml)
    items = []
    for element in root.findall("./channel/item"):
        title = (element.findtext("title") or "").strip()
        traffic_str = element.findtext(f"{{{HTNS}}}approx_traffic") or ""
        m = re.search(r"([\d,.]+)\s*([KMB]?)", traffic_str.upper())
        traffic = int(float(m.group(1).replace(",", "")) * {"": 1, "K": 1000, "M": 1000000, "B": 1000000000}[m.group(2)]) if m else 0
        link = (element.findtext("link") or "").strip()
        if not link:
            link = "https://trends.google.com/trending?" + urlencode({"geo": geo})
        related = []
        for x in element.findall(f"{{{HTNS}}}news_item"):
            related.append({"title": x.findtext(f"{{{HTNS}}}news_item_title") or "", "url": x.findtext(f"{{{HTNS}}}news_item_url") or ""})
        items.append({"title": title, "url": link, "published": parse_when(element.findtext("pubDate")), "kind": "trend", "source": f"Google趋势({geo})", "traffic": traffic, "related": related})
    return items


def parse_news(xml: bytes) -> list[dict]:
    root = ET.fromstring(xml)
    out = []
    for x in root.findall("./channel/item"):
        title = (x.findtext("title") or "").strip()
        link = (x.findtext("link") or "").strip()
        src = x.find("source")
        source = (src.text or "").strip() if src is not None else "Google新闻"
        out.append({"title": title, "url": link, "published": parse_when(x.findtext("pubDate")), "kind": "news", "source": source, "traffic": 0, "related": []})
    return out


def fetch_sources() -> tuple[list[dict], int]:
    all_items: list[dict] = []
    successes = 0
    for geo in ("US", "GB", "SG", "HK"):
        try:
            url = "https://trends.google.com/trending/rss?" + urlencode({"geo": geo})
            parsed = parse_trends(http_get(url), geo)
            all_items.extend(parsed)
            successes += 1
            LOG.info("Trends %s: %s items", geo, len(parsed))
        except Exception as exc:
            LOG.warning("Trends %s unavailable: %s", geo, type(exc).__name__)
    for idx, q in enumerate(NEWS_QUERIES, start=1):
        try:
            lang = "zh-CN" if idx == 4 else "en-US"
            geo = "CN" if idx == 4 else "US"
            ceid = "CN:zh-Hans" if idx == 4 else "US:en"
            url = "https://news.google.com/rss/search?" + urlencode({"q": q, "hl": lang, "gl": geo, "ceid": ceid})
            parsed = parse_news(http_get(url))
            all_items.extend(parsed)
            successes += 1
            LOG.info("News query %s: %s items", idx, len(parsed))
        except Exception as exc:
            LOG.warning("News query %s unavailable: %s", idx, type(exc).__name__)
    return all_items, successes


def key_for(item: dict) -> str:
    return hashlib.sha256(normalized(item["title"]).encode("utf8")).hexdigest()[:18]


def load_state() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf8"))
        if isinstance(state, dict):
            return {"initialized": bool(state.get("initialized")), "seen": state.get("seen", []), "cooldowns": state.get("cooldowns", {})}
    except (ValueError, OSError):
        pass
    return {"initialized": False, "seen": [], "cooldowns": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf8")


def send_message(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID GitHub Secrets")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urlencode({"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}).encode("utf8")
    # Avoid ever printing token, URL, or full response (they may contain details).
    req = Request(url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urlopen(req, timeout=20) as response:
            result = json.loads(response.read())
            if not result.get("ok"):
                raise RuntimeError("Telegram did not accept message")
    except Exception as exc:
        raise RuntimeError(f"Telegram delivery failed: {type(exc).__name__}; check bot START and chat ID") from None


def suggestion(text: str, category: str) -> str:
    if category == "品牌合作":
        return "品牌解约这事，光看声明还不够，我更想知道原话到底是什么。"
    if category == "道歉回应":
        return "道歉已经发了。接下来就看这事是不是能翻篇。"
    if category == "奖项":
        return "奖已经颁了，但到底该不该是他拿，估计又能吵一天。"
    if category == "法律事件":
        return "先看司法文件，网上那些二手截图真不能直接当判决。"
    return "先看看原视频和当事人的完整说法，别急着跟着热搜下结论。"


def render_alert(item: dict, person: str) -> str:
    category = event_type(item["title"])
    published = item["published"]
    time_str = published.astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%m-%d %H:%M") if published else "时间待核实"
    views = f"\nGoogle搜索量级下限（非X曝光）：约{item['traffic']:,}+" if item["kind"] == "trend" and item.get("traffic") else ""
    headline = item["title"][:230]
    extra_source = ""
    if item.get("related"):
        first = item["related"][0]
        if first.get("url"):
            extra_source = f"\n相关媒体报道：{first['url']}"
    return (
        f"🚨 名人热点线索｜{person}｜{category}\n"
        f"来源发布时间（北京时间）：{time_str}\n\n"
        f"{headline}\n"
        f"来源：{item['source']}{views}\n"
        f"来源链接：{item['url']}{extra_source}\n\n"
        f"💬 备选评论（核实后再发）：{suggestion(headline, category)}\n\n"
        "⚠️ 免费公开来源的早期线索，未核实 X 实时热度；争议事实须先核对原始报道。"
    )


def eligible_recent(item: dict, now: dt.datetime) -> bool:
    published = item.get("published")
    if not published:
        return False
    age = (now - published).total_seconds() / 60
    return -12 <= age <= MAX_AGE_MINUTES


def process(items: list[dict], state: dict, now: dt.datetime, *, deliver: bool = True) -> tuple[int, bool]:
    candidates = []
    for item in items:
        if not eligible_recent(item, now):
            continue
        score, person = score_item(item)
        if score >= 4 and person:
            candidates.append((score, item, person))
    candidates.sort(key=lambda v: (v[0], v[1]["published"]), reverse=True)
    if not state["initialized"]:
        # Safe first run: do not dump a backlog of old headlines into Telegram.
        keys = {key_for(x[1]) for x in candidates}
        state["seen"] = list(keys)[:500]
        state["initialized"] = True
        LOG.info("First run: established baseline (%s candidates); no historical alert sent", len(candidates))
        return 0, True

    seen = set(state.get("seen", []))
    cooldowns = dict(state.get("cooldowns", {}))
    sent = 0
    changed = False
    for score, item, person in candidates:
        if sent >= MAX_ALERTS_PER_RUN:
            break
        key = key_for(item)
        if key in seen:
            continue
        group = person + ":" + event_type(item["title"])
        last = parse_when(cooldowns.get(group)) if group in cooldowns else None
        if last and (now - last).total_seconds() < COOLDOWN_HOURS * 3600:
            continue
        if deliver:
            send_message(render_alert(item, person))
        else:
            print(render_alert(item, person))
        sent += 1
        seen.add(key)
        cooldowns[group] = email.utils.format_datetime(now)
        changed = True
    # Last 4000 seen item hashes. Only updated after successfully delivered alerts.
    if changed:
        state["seen"] = list(sorted(seen))[-4000:]
        state["cooldowns"] = {k: v for k, v in cooldowns.items() if (now - (parse_when(v) or now)).total_seconds() < 3 * 86400}
    return sent, changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="Send a test Telegram message only")
    ap.add_argument("--dry-run", action="store_true", help="Show candidates without sending")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.test:
        send_message("✅ 哆啦A梦热点雷达连接成功！\n这是测试消息，不代表发现了真实热点。\n定时监控每15分钟尝试运行一次（GitHub可能延迟）。")
        LOG.info("Test delivered")
        return 0
    items, success_count = fetch_sources()
    if success_count == 0:
        LOG.error("All sources failed: state preserved; nothing sent")
        return 1
    state = load_state()
    sent, changed = process(items, state, now_utc(), deliver=not args.dry_run)
    if changed and not args.dry_run:
        save_state(state)
    LOG.info("Source count=%s, items=%s, sent=%s, state_changed=%s", success_count, len(items), sent, changed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
