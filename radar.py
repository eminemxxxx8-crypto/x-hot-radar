#!/usr/bin/env python3
"""Free public RSS discovery. No X analytics, AI model, or automatic fact verification."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
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
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

LOG = logging.getLogger("hotradar")
UTC = dt.timezone.utc
STATE_PATH = Path("data/state.json")
MAX_AGE_MINUTES = 120
MAX_ALERTS_PER_RUN = 2
RETENTION_HOURS = 72
HTNS = "https://trends.google.com/trending/rss"

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

# This list improves name normalization, not eligibility. Unknown people can enter
# through role-qualified headlines (see get_entity) and broad discovery queries.
ALIASES.update({
    "王嘉尔": ["jackson wang", "王嘉尔"], "周杰伦": ["jay chou", "周杰伦"],
    "肖战": ["肖战", "xiao zhan"], "刘亦菲": ["刘亦菲", "liu yifei"],
    "郑钦文": ["郑钦文", "qinwen zheng"], "孙兴慜": ["son heung-min", "孙兴慜"],
    "阿黛尔": ["adele", "阿黛尔"], "爱莉安娜": ["ariana grande", "爱莉安娜"],
})

BORING_WORDS = (
    "match preview", "predicted lineup", "starting xi", "how to watch",
    "live stream", "fantasy football", "odds", "betting tips", "lineups",
    "transfer rumours", "赛前", "首发名单", "比分直播", "盘口", "赔率", "伤病报告",
)
RETROSPECTIVE = re.compile(
    r"\b(years? ago|months? ago|last year|on this day|throwback|anniversary|"
    r"resurfaces?|resurfaced|revisited|remember when|look back)\b|"
    r"旧闻|回顾|往事|周年|当年|去年|昔日|旧事|翻旧账|旧视频|旧照|重提"
)
RUMOR = re.compile(r"\b(rumou?r|unconfirmed|alleged|allegedly|reportedly|sources say|may|might|could|expected to)\b|传闻|网传|疑似|据传|爆料|被指")
OPINION = re.compile(r"\b(opinion|column|what if|should|might|could)\b|评论：|如果|假如|应该|可能会")
# A request for an apology is not an apology. Contract speculation is not a split.
STAGES = [
    ("道歉回应", r"\b(apolog(?:y|ies|ize[sd]?|ise[sd]?|izing|ising))\b|公开道歉|发表道歉|致歉|道歉"),
    ("品牌解约", r"\b(drops?|dropped|terminat(?:e[sd]?|ion)|cuts? ties|ends? (?:deal|partnership)|sponsor(?:ship)? row)\b|解约|终止合作|撤销代言"),
    ("法律进展", r"\b(arrest(?:ed)?|charged|lawsuit|sued|convicted|acquitted|verdict)\b|被捕|起诉|判决|无罪|拘留"),
    ("公开回应", r"\b(denies|denied|responds|responded|issues statement)\b|公开回应|否认|澄清|发声明"),
    ("突发事件", r"\b(dies|died|retires|retirement|breakup|split|suspended|banned|resigns|fired)\b|去世|退役|分手|停赛|封禁|辞职|解雇"),
    ("争议线索", r"\b(controversy|controversial|backlash|boycott|scandal|outrage)\b|争议|抵制|丑闻|引发质疑"),
]
STAGE_PATTERNS = [(name, re.compile(pattern)) for name, pattern in STAGES]
CONCEPTS = {
    "racism": r"racis|racial|种族|歧视", "betting": r"gambl|betting|赌博|赌球",
    "doping": r"doping|drug test|兴奋剂|药检", "assault": r"assault|暴力|袭击",
    "sexual": r"sexual|harass|性侵|骚扰", "tax": r"tax evasion|逃税|税务",
    "nike": r"\bnike\b|耐克", "adidas": r"\badidas\b|阿迪达斯",
    "pepsi": r"\bpepsi\b|百事", "balenciaga": r"balenciaga|巴黎世家",
    "interview": r"interview|采访", "social-post": r"social media post|deleted post|删帖",
}
STOPWORDS = set("the a an is are was were has have had of to in on for from with over after amid and or as at by it his her their its this that new latest breaking news star actor singer footballer actress player says said say faces facing fans controversy controversial backlash scandal apology apologizes apologises apologised apologized public issues issued response responds responded denies denied demands calls call demand sponsor sponsorship dropped drops deal contract cuts ties terminates termination lawsuit sued charged arrest arrested report reports reportedly today yesterday now statement row under sparks sparked about against following into more out up will not be been he she but all can you we they why how gets get takes take during media interview".split())
# Identity is based on publisher domains in the RSS source metadata, never a
# matching source display name or the Google aggregator domain.
TRUSTED = {
    "reuters.com": "Reuters", "apnews.com": "AP", "bbc.com": "BBC", "bbc.co.uk": "BBC",
    "theguardian.com": "Guardian", "espn.com": "ESPN", "espn.co.uk": "ESPN",
    "skysports.com": "Sky", "variety.com": "Variety", "deadline.com": "Deadline",
    "billboard.com": "Billboard", "people.com": "People", "scmp.com": "SCMP",
    "theathletic.com": "NYT", "nytimes.com": "NYT", "cnn.com": "CNN",
    "cna.com.tw": "CNA", "channelnewsasia.com": "CNA-SG", "news.cn": "Xinhua",
    "xinhuanet.com": "Xinhua", "chinanews.com.cn": "CNS",
}
NEWS_QUERIES = [
    ('"Mbappe" OR "Messi" OR "Cristiano Ronaldo" OR "Yamal" when:1d', "en"),
    ('"Taylor Swift" OR "Justin Bieber" OR "Elon Musk" OR "LeBron James" when:1d', "en"),
    ('(actor OR singer OR footballer OR celebrity) (apology OR backlash OR arrested OR sponsor) when:1d', "en"),
    ('(celebrity OR actress OR rapper OR athlete) (controversy OR boycott OR statement) when:1d', "en"),
    ('(明星 OR 歌手 OR 演员 OR 球星) (道歉 OR 争议 OR 解约 OR 被捕 OR 突发) when:1d', "zh"),
    ('(姆巴佩 OR 梅西 OR C罗 OR 比伯 OR 马斯克 OR 周杰伦 OR 王嘉尔) when:1d', "zh"),
]


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def normalized(text: str) -> str:
    s = unicodedata.normalize("NFKD", html.unescape(text or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def contains_alias(text: str, alias: str) -> bool:
    hay, needle = normalized(text), normalized(alias)
    if needle.isascii() and all(ch.isalnum() or ch == " " for ch in needle):
        return re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", hay) is not None
    return needle in hay


def get_entity(text: str) -> str | None:
    for display, aliases in ALIASES.items():
        if any(contains_alias(text, alias) for alias in aliases):
            return display
    # Conservative open-name discovery: a person role plus a name. No generic
    # capitalized phrase extraction (which mistakes products/teams for people).
    match = re.search(r"\b(?i:actor|actress|singer|rapper|footballer|athlete|tennis player)\s+([A-Z][a-z]+(?:[-'][A-Z]?[a-z]+)?(?:\s+[A-Z][a-z]+){1,2})\b", text)
    if match:
        name = match.group(1).split()
        while len(name) > 2 and (normalized(name[-1]) in STOPWORDS or normalized(name[-1]) in {"makes", "announces", "reveals", "breaks"}):
            name.pop()
        return " ".join(name)
    match = re.search(r"(?:演员|歌手|球星|影星|网球选手)[·\s]*([\u4e00-\u9fff]{2,4})(?=因|被|就|宣布|公开|发表|道歉|回应|涉嫌|遭|解约)", text)
    return match.group(1) if match else None


def event_type(text: str) -> str | None:
    text = normalized(text)
    if re.search(r"denies? (?:ever )?apolog|否认.*道歉", text):
        return "公开回应"
    if re.search(r"(?:demands?|calls? for|urges?|wants?|must|should|refuses? to|won.t|not)\s+(?:\w+\s+){0,3}apolog|要求.*道歉|拒绝道歉|尚未道歉|没有道歉|未道歉|是否.*道歉", text):
        return "争议线索"
    for name, pattern in STAGE_PATTERNS:
        if not pattern.search(text):
            continue
        if name == "品牌解约" and not re.search(r"sponsor|brand|partnership|contract|deal|cuts? ties|nike|adidas|pepsi|balenciaga|品牌|解约|代言|合作", text):
            continue
        return name
    return None


def development(stage: str, title: str) -> str:
    """Keep meaningful judicial updates distinct within the same incident."""
    text = normalized(title)
    if stage == "法律进展":
        for phase, pattern in (("判决", r"convicted|acquitted|verdict|判决|无罪"),
                               ("起诉", r"charged|lawsuit|sued|起诉"),
                               ("被捕", r"arrest|被捕|拘留")):
            if re.search(pattern, text):
                return stage + ":" + phase
    return stage


def parse_when(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        val = email.utils.parsedate_to_datetime(value)
        return val.replace(tzinfo=UTC) if val.tzinfo is None else val.astimezone(UTC)
    except (ValueError, TypeError, IndexError):
        return None


def http_get(url: str, timeout: float = 12) -> bytes:
    req = Request(url, headers={"User-Agent": "HotRadar/2.0 (public RSS reader)", "Accept": "application/rss+xml, text/xml"})
    for attempt in range(2):
        try:
            with urlopen(req, timeout=timeout) as response:
                data = response.read(2_000_000)
                if not data:
                    raise ValueError("empty response")
                return data
        except Exception:
            if attempt:
                # Deliberately do not include URLs, response bodies or exception
                # messages: callers can also use authenticated destinations.
                raise RuntimeError("RSS request failed") from None
            time.sleep(1)
    raise AssertionError("unreachable")


def parse_trends(xml: bytes, geo: str) -> list[dict]:
    root = ET.fromstring(xml)
    items = []
    for element in root.findall("./channel/item"):
        related = [{"title": x.findtext(f"{{{HTNS}}}news_item_title") or "",
                    "url": x.findtext(f"{{{HTNS}}}news_item_url") or ""}
                   for x in element.findall(f"{{{HTNS}}}news_item")]
        items.append({"title": (element.findtext("title") or "").strip(),
                      "published": parse_when(element.findtext("pubDate")),
                      "kind": "trend", "related": related, "source": f"Google趋势({geo})"})
    return items


def parse_news(xml: bytes) -> list[dict]:
    root = ET.fromstring(xml)
    out = []
    for x in root.findall("./channel/item"):
        src = x.find("source")
        source = (src.text or "").strip() if src is not None else "未知媒体"
        title = html.unescape((x.findtext("title") or "").strip())
        if source and title.endswith(" - " + source):
            title = title[:-len(source)-3]
        out.append({"title": title, "url": (x.findtext("link") or "").strip(),
                    "published": parse_when(x.findtext("pubDate")), "kind": "news",
                    "source": source, "source_url": src.get("url", "") if src is not None else ""})
    return out


def fetch_sources() -> tuple[list[dict], int]:
    jobs = [("Trends " + geo, "https://trends.google.com/trending/rss?" + urlencode({"geo": geo}), geo)
            for geo in ("US", "GB", "SG", "HK")]
    for idx, (query, lang) in enumerate(NEWS_QUERIES):
        params = {"q": query, "hl": "zh-CN" if lang == "zh" else "en-US",
                  "gl": "CN" if lang == "zh" else "US", "ceid": "CN:zh-Hans" if lang == "zh" else "US:en"}
        jobs.append((f"News {idx+1}", "https://news.google.com/rss/search?" + urlencode(params), None))

    def fetch(job):
        label, url, geo = job
        try:
            data = http_get(url)
            parsed = parse_trends(data, geo) if geo else parse_news(data)
            LOG.info("%s: %d items", label, len(parsed))
            return parsed, 1
        except Exception as exc:
            LOG.warning("%s unavailable: %s", label, type(exc).__name__)
            return [], 0

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fetch, jobs))
    return [item for items, _ in results for item in items], sum(ok for _, ok in results)


def key_for(item: dict) -> str:
    return hashlib.sha256(normalized(item["title"]).encode("utf8")).hexdigest()[:18]


def empty_state() -> dict:
    return {"version": 2, "initialized": False, "seen": [], "events": []}


def load_state() -> dict:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf8"))
    except FileNotFoundError:
        return empty_state()
    except (ValueError, OSError):
        raise RuntimeError("State unreadable; refusing to discard deduplication history") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("seen", []), list) or not all(isinstance(key, str) for key in raw.get("seen", [])):
        raise RuntimeError("Invalid state")
    if raw.get("version") not in (None, 1, 2):
        raise RuntimeError("Unsupported state version")
    if raw.get("version") != 2:
        # Legacy hashes cannot reconstruct semantic history. Seed the first v2
        # batch silently, retain hashes, and never treat old cooldowns as events.
        return {**empty_state(), "seen": raw.get("seen", []), "migration": True}
    if not isinstance(raw.get("events"), list):
        raise RuntimeError("Invalid event history")
    for event in raw["events"]:
        if not isinstance(event, dict) or not all(k in event for k in ("id", "person", "anchors", "concepts", "first_seen", "published", "stages", "reports")):
            raise RuntimeError("Invalid event record")
        if not isinstance(event["person"], str) or not isinstance(event["id"], str):
            raise RuntimeError("Invalid event identity")
        for key in ("anchors", "concepts", "stages"):
            if not isinstance(event[key], list) or not all(isinstance(value, str) for value in event[key]):
                raise RuntimeError("Invalid event fields")
        if not isinstance(event["reports"], list):
            raise RuntimeError("Invalid report history")
        for report in event["reports"]:
            if not isinstance(report, dict) or not all(isinstance(report.get(key), str) for key in ("key", "publisher", "stage", "published")) or not parse_when(report["published"]):
                raise RuntimeError("Invalid report")
            if not isinstance(report.get("trusted"), bool) or not isinstance(report.get("rumor"), bool):
                raise RuntimeError("Invalid report status")
        if not parse_when(event["first_seen"]) or not parse_when(event["published"]):
            raise RuntimeError("Invalid event timestamps")
    return raw


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf8")
    tmp.replace(STATE_PATH)


def safe_url(url: str) -> str:
    try:
        if len(url) > 1800 or any(ch.isspace() or ord(ch) < 32 for ch in url):
            return ""
        parts = urlsplit(url)
        return url if parts.scheme == "https" and parts.hostname and not parts.username and not parts.password else ""
    except ValueError:
        return ""


def publisher(item: dict) -> tuple[str, bool]:
    url = safe_url(item.get("source_url", ""))
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    if host in {"news.google.com", "trends.google.com"}:
        return "", False
    group = next((group for domain, group in TRUSTED.items() if host == domain or host.endswith("." + domain)), None)
    return (group or host), group is not None


def anchors_for(title: str, person: str) -> tuple[set[str], set[str]]:
    text = normalized(title)
    for alias in ALIASES.get(person, [person]):
        text = text.replace(normalized(alias), " ")
    concepts = {name for name, pattern in CONCEPTS.items() if re.search(pattern, text)}
    words = {w for w in re.findall(r"[a-z]{3,}", text) if w not in STOPWORDS}
    # Character bigrams permit limited Chinese paraphrase matching without NLP
    # dependencies. This is conservative lexical grouping, not semantic AI.
    for span in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        words.update(span[i:i+2] for i in range(len(span)-1))
    return words, concepts


def same_event(a: dict, b: dict) -> bool:
    if a["person"] != b["person"]:
        return False
    ac, bc = set(a["concepts"]), set(b["concepts"])
    # Different named brands/controversy topics should not share a person timer.
    brands = {"nike", "adidas", "pepsi", "balenciaga"}
    if ac & brands and bc & brands and not (ac & bc & brands):
        return False
    if ac and bc and not ac & bc:
        return False
    aw, bw = set(a["anchors"]), set(b["anchors"])
    overlap = len(aw & bw) / max(1, min(len(aw), len(bw)))
    # "Interview" alone is a medium, not an incident identity.
    return bool((ac & bc) - {"interview", "social-post"}) or (len(aw & bw) >= 2 and overlap >= .5) or (not aw and not bw)


def eligible_recent(item: dict, now: dt.datetime) -> bool:
    published = item.get("published")
    return isinstance(published, dt.datetime) and -5 <= (now-published).total_seconds()/60 <= MAX_AGE_MINUTES


def candidate(item: dict, now: dt.datetime) -> dict | None:
    title = item.get("title", "")
    text = normalized(title)
    if item.get("kind") != "news" or not eligible_recent(item, now):
        return None
    if title.rstrip().endswith(("?", "？")) or RETROSPECTIVE.search(text) or OPINION.search(text) or any(w in text for w in BORING_WORDS):
        return None
    person, stage = get_entity(title), event_type(title)
    group, trusted = publisher(item)
    if not person or not stage or not group or not safe_url(item.get("url", "")):
        return None
    anchors, concepts = anchors_for(title, person)
    return {"person": person, "stage": stage, "phase": development(stage, title), "anchors": sorted(anchors), "concepts": sorted(concepts),
            "item": item, "publisher": group, "trusted": trusted,
            "rumor": bool(RUMOR.search(text)), "key": key_for(item)}


def send_message(text: str) -> None:
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    payload = urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
    req = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload,
                  headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urlopen(req, timeout=20) as response:
            if not json.loads(response.read()).get("ok"):
                raise RuntimeError("delivery rejected")
    except Exception as exc:
        raise RuntimeError(f"Telegram delivery failed: {type(exc).__name__}") from None


def suggestion(person: str, stage: str, rumor: bool = False) -> str:
    if rumor:
        return f"{person}这条消息我先存疑，等当事人或可靠报道说清楚再聊。"
    angles = {
        "道歉回应": "我更在意回应有没有讲清楚具体问题。态度是一回事，后面怎么做才见分晓。",
        "品牌解约": "我想先看品牌给的理由，别把商业决定直接当成事实判定。",
        "法律进展": "有程序上的进展，也不等于网上每个说法都坐实了。具体还是看文件。",
        "公开回应": "回应里哪些说法有证据，哪些只是表态？我比较在意这个。",
        "突发事件": "这条先看完整报道，标题里没交代的细节，暂时不补脑。",
        "争议线索": "我想看完整上下文。只凭截出来的一句话，很容易聊偏。",
    }
    return f"聊{person}这件事，" + angles[stage]


def render_alert(item: dict, person: str, *, stage: str | None = None, sources: list[dict] | None = None,
                 rumor: bool = False, trend: bool = False) -> str:
    stage = stage or event_type(item["title"]) or "争议线索"
    esc = html.escape
    when = item["published"].astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%m-%d %H:%M")
    headline = item["title"][:180]
    chinese = bool(re.search(r"[\u4e00-\u9fff]", headline))
    content = headline if chinese else f"{person}出现{stage}相关报道。\n原文：{headline}"
    links = []
    for source in (sources or [item])[:3]:
        url = safe_url(source.get("url", ""))
        if url:
            links.append(f'<a href="{esc(url, quote=True)}">{esc(source.get("source", "来源")[:40])}</a>')
    label = "传闻/指控，尚未证实" if rumor else "媒体报道，尚未独立核实"
    return (f"<b>{esc(person)} · {esc(stage)}</b>\n{esc(content)}\n\n"
            f"报道：{when}（北京时间）\n事件发生时间：RSS 未提供，待核实\n"
            f"性质：{label}\n来源：{' · '.join(links)}\n"
            + ("发现信号：关联报道进入 Google 趋势\n" if trend else "")
            + f"\n发帖角度（规则草稿，属个人评价）：\n{esc(suggestion(person, stage, rumor))}\n"
            "\n未测量 X 热度；多家刊载不等于独立证实。")


def process(items: list[dict], state: dict, now: dt.datetime, *, deliver: bool = True, checkpoint=None) -> tuple[int, bool]:
    before = copy.deepcopy(state)
    events = state.setdefault("events", [])
    # TTL is anchored to the original observation, never refreshed by rewrites.
    events[:] = [e for e in events if (now-parse_when(e["first_seen"])).total_seconds() < RETENTION_HOURS*3600]
    baseline = not state.get("initialized", False)
    seen = set(state.get("seen", []))
    candidates = [c for item in items if (c := candidate(item, now))]
    candidates.sort(key=lambda c: (c["item"]["published"], c["key"]))
    batches = {}
    for c in candidates:
        event = next((e for e in events if same_event(e, c)), None)
        if event is None:
            event = {"id": c["key"], "person": c["person"], "anchors": c["anchors"], "concepts": c["concepts"],
                     "first_seen": email.utils.format_datetime(now), "published": email.utils.format_datetime(c["item"]["published"]),
                     "stages": [], "reports": []}
            events.append(event)
        # Keep bounded corroboration records across runs. Identical headlines and
        # explicit wire attribution count once even when republished elsewhere.
        title_norm = normalized(c["item"]["title"])
        wire = next((name for name in ("reuters", "associated press", "美联社", "路透") if name in title_norm), None)
        record = {"key": c["key"], "publisher": wire or c["publisher"], "stage": c["stage"], "phase": c["phase"],
                  "source": c["item"]["source"], "url": c["item"]["url"],
                  "trusted": c["trusted"], "rumor": c["rumor"], "published": email.utils.format_datetime(c["item"]["published"])}
        existing = next((r for r in event["reports"] if r["key"] == c["key"]), None)
        if existing is None:
            event["reports"].append(record)
            event["reports"] = event["reports"][-50:]
        elif c["trusted"] and not existing["trusted"]:
            existing.update(record)
        batches.setdefault((event["id"], c["phase"]), []).append((c, event))
    ranked = []
    for (_, phase), entries in batches.items():
        c, event = max(entries, key=lambda pair: (pair[0]["trusted"], not pair[0]["rumor"], pair[0]["item"]["published"]))
        if phase in event["stages"]:
            continue
        if baseline or any(x[0]["key"] in seen for x in entries):
            event["stages"].append(phase)
            continue
        # Only corroborate the proposed development, not old reports about an
        # earlier phase. A demand for apology cannot confirm an actual apology.
        reports = [r for r in event["reports"] if r.get("phase", r["stage"]) == phase and
                   -5 <= (now-parse_when(r["published"])).total_seconds()/60 <= MAX_AGE_MINUTES]
        publishers = {r["publisher"] for r in reports}
        trusted = any(r["trusted"] for r in reports)
        age = (now-c["item"]["published"]).total_seconds()/60
        strong = c["stage"] != "争议线索" and not c["rumor"]
        if not trusted or (len(publishers) < 2 and not (c["trusted"] and strong and age <= 45)):
            continue
        # Correlation to an actual linked headline, not merely the same person.
        trend = any(eligible_recent(t, now) and any(
            key_for({"title": r["title"]}) == c["key"] for r in t.get("related", []))
            for t in items if t.get("kind") == "trend")
        score = (5 if age <= 30 else 3 if age <= 60 else 1) + min(len(publishers), 3) + (2 if trend else 0) + (2 if strong else 0)
        ranked.append((score, c, event, entries, trend))
    ranked.sort(key=lambda x: (x[1]["stage"] != "争议线索", x[0], x[1]["item"]["published"]), reverse=True)
    state.update(version=2, initialized=True, last_run=email.utils.format_datetime(now))
    state.pop("migration", None)
    state.pop("cooldowns", None)
    sent = 0
    for _, c, event, entries, trend in ranked:
        if sent >= MAX_ALERTS_PER_RUN:
            break
        # Another batch may have marked this stage during migration.
        if c["phase"] in event["stages"]:
            continue
        sources = [c["item"]]
        source_groups = {c["publisher"]}
        for report in event["reports"]:
            if report.get("phase", report["stage"]) == c["phase"] and report["publisher"] not in source_groups and report.get("url"):
                sources.append(report)
                source_groups.add(report["publisher"])
        text = render_alert(c["item"], c["person"], stage=c["stage"], sources=sources, rumor=c["rumor"], trend=trend)
        if deliver:
            send_message(text)
        else:
            print(text)
        event["stages"].append(c["phase"])
        predecessors = {"争议线索"} if c["stage"] != "争议线索" else set()
        if c["phase"] == "道歉回应":
            predecessors.add("公开回应")
        elif c["phase"] == "法律进展:判决":
            predecessors.update({"法律进展:被捕", "法律进展:起诉"})
        elif c["phase"] == "法律进展:起诉":
            predecessors.add("法律进展:被捕")
        for report in event["reports"]:
            phase = report.get("phase", report["stage"])
            if phase in predecessors and phase not in event["stages"] and parse_when(report["published"]) <= c["item"]["published"]:
                event["stages"].append(phase)
        state["seen"] = list(dict.fromkeys(state.get("seen", []) + [x[0]["key"] for x in entries]))[-4000:]
        sent += 1
        # Persist after each accepted delivery so a later failure cannot lose the
        # successful prefix. API timeout ambiguity is documented; no blind retry.
        if deliver and checkpoint:
            checkpoint(state)
    LOG.info("Filter: raw=%d candidates=%d events=%d eligible_updates=%d alerts=%d mode=%s baseline=%s", len(items), len(candidates), len(events), len(ranked), sent, "delivery" if deliver else "preview", baseline)
    return sent, state != before


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="Send a real Telegram test message")
    ap.add_argument("--dry-run", action="store_true", help="Preview without sending or saving state")
    ap.add_argument("--state", type=Path, help="Override state location for isolated validation")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    global STATE_PATH
    if args.state:
        STATE_PATH = args.state
    if args.test and args.dry_run:
        ap.error("--test and --dry-run are mutually exclusive")
    if args.test:
        send_message("✅ 热点雷达连接测试成功。此消息不是新闻提醒。")
        return 0
    state = load_state()
    items, success_count = fetch_sources()
    if not any(item.get("kind") == "news" for item in items):
        LOG.error("No news items available: state preserved; trend feeds alone cannot validate events")
        return 1
    sent, changed = process(items, state, now_utc(), deliver=not args.dry_run, checkpoint=save_state)
    if changed and not args.dry_run:
        save_state(state)
    LOG.info("Source count=%d items=%d alerts=%d mode=%s state_saved=%s", success_count, len(items), sent, "preview" if args.dry_run else "delivery", changed and not args.dry_run)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        LOG.error("Run failed: %s (details suppressed to protect credentials)", type(exc).__name__)
        sys.exit(1)
