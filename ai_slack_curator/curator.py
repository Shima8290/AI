#!/usr/bin/env python3
"""AI news/video curator for Slack.

The script is designed for GitHub Actions:
- collect candidates from fixed, trusted sources
- let Gemini select and summarize
- validate the final Slack message
- post through Slack Incoming Webhooks
- persist seen URLs to avoid repeats
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


JST = ZoneInfo("Asia/Tokyo")
ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / ".ai-curator-state.json"
USER_AGENT = "student-ai-school-curator/1.0 (+https://github.com)"

NEWS_WEBHOOK_ENV = "SLACK_WEBHOOK_NEWS"
VIDEO_WEBHOOK_ENV = "SLACK_WEBHOOK_VIDEO"

RSS_FEEDS = [
    "https://www.anthropic.com/news/rss.xml",
    "https://openai.com/news/rss.xml",
    "https://blog.google/technology/ai/rss/",
    "https://deepmind.google/discover/blog/rss.xml",
    "https://rss.itmedia.co.jp/rss/2.0/aiplus.xml",
    "https://www.publickey1.jp/atom.xml",
    "https://ledge.ai/feed/",
]

GOOGLE_NEWS_RSS_QUERIES = [
    "AI when:2d site:itmedia.co.jp/aiplus",
    "生成AI when:2d site:itmedia.co.jp/aiplus",
    "AI when:2d site:publickey1.jp",
    "生成AI when:2d site:ledge.ai",
    "AI when:2d site:xtech.nikkei.com",
    "生成AI when:2d site:nikkei.com",
    "OpenAI when:2d",
    "Anthropic Claude when:2d",
    "Google DeepMind Gemini when:2d",
]

VIDEO_QUERIES_SHORT = [
    "AI",
    "artificial intelligence",
    "AI education",
    "AI future",
    "AI jobs",
]

VIDEO_QUERIES_LONG = [
    "AI",
    "artificial intelligence",
    "OpenAI",
    "Anthropic",
    "Gemini",
]

YOUTUBE_CHANNELS = {
    "lex_fridman": "UCSHZKyawb77ixDdsGog4iWA",
    "all_in": "UCESLZhusAkFfsNsApnjF_Cg",
    "ted": "UCAuUUnT6oDeKwE6v1NGQxug",
    "tedx": "UCsT0YIqwnpJCM-mx7-gSA4Q",
    "ted_ed": "UCsooa4yRKGN_zEE8iknghZA",
    "stanford_gsb": "UCGwuxdEeCf0TIA2RbPOj-8g",
    "wef": "UCw-kH-Od73XDAt7qtH9uBYA",
}

STANFORD_VIEW_FROM_THE_TOP_PLAYLIST = "PLxq_lXOUlvQAwaY_9K4ZFH9Xdar9WzCaL"

SHORT_SOURCE_CHANNEL_IDS = {
    YOUTUBE_CHANNELS["ted"],
    YOUTUBE_CHANNELS["tedx"],
    YOUTUBE_CHANNELS["ted_ed"],
    YOUTUBE_CHANNELS["stanford_gsb"],
    YOUTUBE_CHANNELS["wef"],
}

LONG_SOURCE_CHANNEL_IDS = {
    YOUTUBE_CHANNELS["lex_fridman"],
    YOUTUBE_CHANNELS["all_in"],
    YOUTUBE_CHANNELS["stanford_gsb"],
    YOUTUBE_CHANNELS["wef"],
}

ALLOWED_SHORT_CHANNELS = [
    "TED",
    "TED-Ed",
    "TEDx Talks",
    "World Economic Forum",
    "OpenAI",
    "Google DeepMind",
    "Anthropic",
    "Stanford Graduate School of Business",
    "Lex Clips",
    "Lex Fridman",
    "All-In Podcast",
]

ALLOWED_LONG_CHANNELS = [
    "Lex Fridman",
    "All-In Podcast",
    "Stanford Graduate School of Business",
    "World Economic Forum",
]

WEEKDAY_THEMES = {
    0: "AI × ビジネス・キャリア",
    1: "AI × 技術（モデル仕組み、実用Tips）",
    2: "AI × 社会・倫理",
    3: "AI × 学業・学習効率",
    4: "AI × クリエイティブ・ユース（音楽、映像、デザイン）",
}


@dataclass
class Candidate:
    title: str
    url: str
    source: str
    published: str = ""
    snippet: str = ""
    duration_seconds: int | None = None
    channel: str = ""
    channel_id: str = ""

    def normalized_url(self) -> str:
        parsed = urllib.parse.urlsplit(self.url)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        filtered = [(k, v) for k, v in query if not k.lower().startswith("utm_")]
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(filtered), "")
        )


def request_json(url: str, *, method: str = "GET", data: dict[str, Any] | None = None) -> dict[str, Any]:
    body = None
    headers = {"User-Agent": USER_AGENT}
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def request_raw(url: str, *, method: str = "GET", data: dict[str, Any] | None = None) -> str:
    body = None
    headers = {"User-Agent": USER_AGENT}
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def request_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read(1_500_000)
    charset = resp.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"seen": {}}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def seen_urls(state: dict[str, Any], bucket: str, days: int) -> set[str]:
    cutoff = dt.datetime.now(JST) - dt.timedelta(days=days)
    seen = set()
    for url, iso_date in state.get("seen", {}).get(bucket, {}).items():
        try:
            ts = dt.datetime.fromisoformat(iso_date)
        except ValueError:
            continue
        if ts >= cutoff:
            seen.add(url)
    return seen


def mark_seen(state: dict[str, Any], bucket: str, urls: list[str]) -> None:
    state.setdefault("seen", {}).setdefault(bucket, {})
    now = dt.datetime.now(JST).isoformat()
    for url in urls:
        state["seen"][bucket][url] = now


def clean_state(state: dict[str, Any], keep_days: int = 60) -> None:
    cutoff = dt.datetime.now(JST) - dt.timedelta(days=keep_days)
    for bucket, values in list(state.get("seen", {}).items()):
        for url, iso_date in list(values.items()):
            try:
                ts = dt.datetime.fromisoformat(iso_date)
            except ValueError:
                ts = cutoff - dt.timedelta(days=1)
            if ts < cutoff:
                del values[url]


def parse_date(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip()
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(JST).date().isoformat()
    except Exception:
        pass
    for pattern in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(value[:10], pattern).date().isoformat()
        except Exception:
            continue
    match = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", value)
    if match:
        y, m, d = map(int, match.groups())
        return dt.date(y, m, d).isoformat()
    return ""


def cse_search(query: str, *, days: int, num: int = 10) -> list[Candidate]:
    key = os.getenv("GOOGLE_CSE_API_KEY")
    cx = os.getenv("GOOGLE_CSE_ID")
    if not key or not cx:
        return []
    params = {
        "key": key,
        "cx": cx,
        "q": query,
        "num": min(num, 10),
        "dateRestrict": f"d{days}",
        "safe": "active",
    }
    url = "https://www.googleapis.com/customsearch/v1?" + urllib.parse.urlencode(params)
    try:
        data = request_json(url)
    except Exception as exc:
        print(f"warning: CSE failed for {query!r}: {exc}", file=sys.stderr)
        return []
    candidates = []
    for item in data.get("items", []):
        pagemap = item.get("pagemap", {})
        metatags = (pagemap.get("metatags") or [{}])[0]
        published = (
            metatags.get("article:published_time")
            or metatags.get("date")
            or metatags.get("publishdate")
            or ""
        )
        candidates.append(
            Candidate(
                title=html.unescape(item.get("title", "")).strip(),
                url=item.get("link", "").strip(),
                source=item.get("displayLink", "").strip(),
                published=parse_date(published),
                snippet=html.unescape(item.get("snippet", "")).strip(),
            )
        )
    return candidates


def rss_candidates(feed_url: str) -> list[Candidate]:
    try:
        text = request_text(feed_url)
        root = ET.fromstring(text)
    except Exception as exc:
        print(f"warning: RSS failed for {feed_url}: {exc}", file=sys.stderr)
        return []
    candidates = []
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "dc": "http://purl.org/dc/elements/1.1/",
    }
    items = root.findall(".//item") or root.findall(".//atom:entry", ns)
    for item in items[:20]:
        title = find_text(item, ["title", "atom:title"], ns)
        link = find_text(item, ["link"], ns)
        if not link:
            atom_link = item.find("atom:link", ns)
            link = atom_link.attrib.get("href", "") if atom_link is not None else ""
        published = find_text(item, ["pubDate", "published", "updated", "atom:published", "atom:updated"], ns)
        description = find_text(item, ["description", "summary", "atom:summary"], ns)
        if title and link:
            candidates.append(
                Candidate(
                    title=html.unescape(strip_tags(title)).strip(),
                    url=link.strip(),
                    source=urllib.parse.urlsplit(feed_url).netloc,
                    published=parse_date(published),
                    snippet=html.unescape(strip_tags(description)).strip()[:300],
                )
            )
    return candidates


def google_news_rss_url(query: str) -> str:
    params = {
        "q": query,
        "hl": "ja",
        "gl": "JP",
        "ceid": "JP:ja",
    }
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)


def find_text(node: ET.Element, names: list[str], ns: dict[str, str]) -> str:
    for name in names:
        found = node.find(name, ns) if ":" in name else node.find(name)
        if found is not None and found.text:
            return found.text
    return ""


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "")


def dedupe(candidates: list[Candidate]) -> list[Candidate]:
    seen = set()
    output = []
    for cand in candidates:
        if not cand.url or not cand.title:
            continue
        key = cand.normalized_url()
        if key in seen:
            continue
        seen.add(key)
        output.append(cand)
    return output


def collect_news_candidates(days: int = 2) -> list[Candidate]:
    candidates: list[Candidate] = []
    for feed in RSS_FEEDS:
        candidates.extend(rss_candidates(feed))
    for query in GOOGLE_NEWS_RSS_QUERIES:
        candidates.extend(rss_candidates(google_news_rss_url(query)))
        time.sleep(0.1)
    return dedupe(candidates)


def youtube_search(query: str, *, days: int, max_results: int = 8, channel_id: str | None = None) -> list[str]:
    key = os.getenv("YOUTUBE_API_KEY")
    if not key:
        return []
    after = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat().replace("+00:00", "Z")
    params = {
        "part": "snippet",
        "type": "video",
        "order": "date",
        "q": query,
        "publishedAfter": after,
        "maxResults": max_results,
        "key": key,
        "safeSearch": "strict",
        "relevanceLanguage": "en",
    }
    if channel_id:
        params["channelId"] = channel_id
    url = "https://www.googleapis.com/youtube/v3/search?" + urllib.parse.urlencode(params)
    try:
        data = request_json(url)
    except Exception as exc:
        print(f"warning: YouTube search failed for {query!r}: {exc}", file=sys.stderr)
        return []
    ids = []
    for item in data.get("items", []):
        video_id = item.get("id", {}).get("videoId")
        if video_id:
            ids.append(video_id)
    return ids


def youtube_playlist_video_ids(playlist_id: str, *, max_results: int = 25) -> list[str]:
    key = os.getenv("YOUTUBE_API_KEY")
    if not key:
        return []
    params = {
        "part": "contentDetails",
        "playlistId": playlist_id,
        "maxResults": max_results,
        "key": key,
    }
    url = "https://www.googleapis.com/youtube/v3/playlistItems?" + urllib.parse.urlencode(params)
    try:
        data = request_json(url)
    except Exception as exc:
        print(f"warning: YouTube playlist failed for {playlist_id!r}: {exc}", file=sys.stderr)
        return []
    ids = []
    for item in data.get("items", []):
        video_id = item.get("contentDetails", {}).get("videoId")
        if video_id:
            ids.append(video_id)
    return ids


def youtube_videos(video_ids: list[str]) -> list[Candidate]:
    key = os.getenv("YOUTUBE_API_KEY")
    if not key or not video_ids:
        return []
    candidates = []
    for chunk_start in range(0, len(video_ids), 50):
        chunk = video_ids[chunk_start : chunk_start + 50]
        params = {
            "part": "snippet,contentDetails",
            "id": ",".join(chunk),
            "key": key,
            "maxResults": 50,
        }
        url = "https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(params)
        try:
            data = request_json(url)
        except Exception as exc:
            print(f"warning: YouTube videos failed: {exc}", file=sys.stderr)
            continue
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            duration = parse_iso_duration(item.get("contentDetails", {}).get("duration", ""))
            candidates.append(
                Candidate(
                    title=snippet.get("title", ""),
                    url=f"https://www.youtube.com/watch?v={item.get('id')}",
                    source="YouTube",
                    published=parse_date(snippet.get("publishedAt", "")),
                    snippet=snippet.get("description", "")[:400],
                    duration_seconds=duration,
                    channel=snippet.get("channelTitle", ""),
                    channel_id=snippet.get("channelId", ""),
                )
            )
    return candidates


def parse_iso_duration(value: str) -> int | None:
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not match:
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def collect_video_candidates(*, weekly: bool, days: int | None = None) -> list[Candidate]:
    queries = VIDEO_QUERIES_LONG if weekly else VIDEO_QUERIES_SHORT
    channel_ids = LONG_SOURCE_CHANNEL_IDS if weekly else SHORT_SOURCE_CHANNEL_IDS
    if weekly:
        queries = VIDEO_QUERIES_LONG + VIDEO_QUERIES_SHORT
        channel_ids = LONG_SOURCE_CHANNEL_IDS | SHORT_SOURCE_CHANNEL_IDS
    days = days or (7 if weekly else 45)
    ids: list[str] = []
    for channel_id in channel_ids:
        for query in queries:
            ids.extend(youtube_search(query, days=days, max_results=6, channel_id=channel_id))
            time.sleep(0.1)
    if weekly:
        ids.extend(youtube_playlist_video_ids(STANFORD_VIEW_FROM_THE_TOP_PLAYLIST, max_results=25))
    candidates = youtube_videos(list(dict.fromkeys(ids)))
    candidates = [c for c in candidates if is_allowed_video_candidate(c, weekly=weekly)]
    if weekly:
        return dedupe(candidates)
    return dedupe([c for c in candidates if c.duration_seconds and 600 <= c.duration_seconds <= 1200])


def is_allowed_video_candidate(candidate: Candidate, *, weekly: bool) -> bool:
    allowed_ids = (SHORT_SOURCE_CHANNEL_IDS | LONG_SOURCE_CHANNEL_IDS) if weekly else SHORT_SOURCE_CHANNEL_IDS
    if candidate.channel_id not in allowed_ids:
        return False
    if is_disallowed_language(candidate.title) or is_disallowed_language(candidate.snippet):
        return False
    title = f"{candidate.title} {candidate.snippet}".casefold()
    ai_terms = ["ai", "artificial intelligence", "生成ai", "人工知能", "gemini", "openai", "anthropic", "claude", "deepmind", "llm"]
    return any(term in title for term in ai_terms)


def is_disallowed_language(text: str) -> bool:
    if not text:
        return False
    sample = text[:160]
    disallowed = sum(1 for char in sample if is_disallowed_script(char))
    letters = sum(1 for char in sample if char.isalpha())
    return letters > 0 and disallowed / max(letters, 1) > 0.15


def is_disallowed_script(char: str) -> bool:
    code = ord(char)
    return (
        0x0400 <= code <= 0x04FF  # Cyrillic
        or 0x0E00 <= code <= 0x0E7F  # Thai
        or 0xAC00 <= code <= 0xD7AF  # Hangul
        or 0x0600 <= code <= 0x06FF  # Arabic
    )


def gemini_generate(prompt: str) -> str:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    model = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"
    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{urllib.parse.quote(model)}:generateContent?key={urllib.parse.quote(key)}"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.35,
            "topP": 0.9,
            "responseMimeType": "application/json",
        },
    }
    data = request_json(endpoint, method="POST", data=payload)
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as exc:
        raise RuntimeError(f"Unexpected Gemini response: {data}") from exc


def extract_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
        if match:
            return json.loads(match.group(1))
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end >= start:
            return json.loads(text[start : end + 1])
        raise


def candidates_for_prompt(candidates: list[Candidate], limit: int = 30) -> str:
    compact = []
    for cand in candidates[:limit]:
        item = asdict(cand)
        item["duration"] = format_duration(cand.duration_seconds) if cand.duration_seconds else ""
        compact.append(item)
    return json.dumps(compact, ensure_ascii=False, indent=2)


def build_news_message(candidates: list[Candidate], state: dict[str, Any]) -> tuple[str, list[str]]:
    blocked = seen_urls(state, "news", 14)
    candidates = [c for c in candidates if c.normalized_url() not in blocked]
    candidates = diversify_candidates(candidates, per_domain=4)
    if len(candidates) < 5:
        candidates = collect_news_candidates(days=4)
        candidates = [c for c in candidates if c.normalized_url() not in blocked]
        candidates = diversify_candidates(candidates, per_domain=4)
    today = dt.datetime.now(JST)
    prompt = f"""
あなたは「学生AIスクール」コミュニティ向けのAIニュースキュレーターです。
候補から重要度・鮮度・学生への関係性・日本/海外の多様性で最大5本を選び、Slack投稿を作るためのJSONだけ返してください。

今日: {today.strftime('%Y/%-m/%-d')}({jp_weekday(today)})
条件:
- 当日・前日・過去48時間のAI関連ニュースを優先
- 日本語/日本国内ソース候補がある場合は1〜2本入れる
- 同じ企業・同じドメインに偏らせない。OpenAI/Google/Anthropic公式だけで5本にしない
- 重複や根拠の薄い記事は避ける
- 本文は各80〜150字、2〜4文
- 全て日本語
- 架空の内容を足さない。不確かな場合は候補から外す

返却JSON:
{{"items":[{{"title":"...","date":"YYYY/M/D","body":"...","source":"...","url":"..."}}]}}

候補:
{candidates_for_prompt(candidates, 40)}
"""
    try:
        data = extract_json(gemini_generate(prompt))
        items = data.get("items", [])[:5]
    except Exception as exc:
        print(f"warning: Gemini news generation failed: {exc}", file=sys.stderr)
        items = fallback_news_items(candidates[:5])

    if len(items) < 2:
        message = (
            f"📰 **AIニュース朝報** — {today.strftime('%Y/%-m/%-d')}({jp_weekday(today)})\n\n"
            "本日は特筆すべき大型ニュースなし。注目トピックを2本だけお届け。\n\n"
            "---\n_明日も朝7:00に配信_"
        )
        return message, []

    lines = [f"📰 **AIニュース朝報** — {today.strftime('%Y/%-m/%-d')}({jp_weekday(today)})", ""]
    used_urls = []
    for i, item in enumerate(items, 1):
        title = sanitize_inline(item.get("title", ""))
        date = sanitize_inline(item.get("date") or today.strftime("%Y/%-m/%-d"))
        body = sanitize_body(item.get("body", ""))
        source = sanitize_inline(item.get("source", "出典"))
        url = item.get("url", "").strip()
        if not title or not body or not url:
            continue
        lines.extend(
            [
                f"**{i}. {title}** _({date})_",
                body,
                f"[{source}]({url})",
                "",
            ]
        )
        used_urls.append(normalize_url(url))
    lines.extend(["---", "_明日も朝7:00に配信_"])
    message = "\n".join(lines)
    validate_message(message, expected_emoji="📰")
    return message, used_urls


def diversify_candidates(candidates: list[Candidate], *, per_domain: int) -> list[Candidate]:
    buckets: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        domain = urllib.parse.urlsplit(candidate.url).netloc.replace("www.", "")
        buckets.setdefault(domain, [])
        if len(buckets[domain]) < per_domain:
            buckets[domain].append(candidate)
    output: list[Candidate] = []
    while buckets:
        for domain in list(buckets.keys()):
            bucket = buckets[domain]
            if bucket:
                output.append(bucket.pop(0))
            if not bucket:
                del buckets[domain]
    return output


def fallback_news_items(candidates: list[Candidate]) -> list[dict[str, str]]:
    items = []
    for c in candidates:
        items.append(
            {
                "title": c.title[:80],
                "date": slash_date(c.published),
                "body": (c.snippet or "AI関連の最新動向です。学生は学業・就活・キャリアへの影響を確認しておきたい内容です。")[:150],
                "source": c.source or urllib.parse.urlsplit(c.url).netloc,
                "url": c.url,
            }
        )
    return items


def build_daily_video_message(candidates: list[Candidate], state: dict[str, Any]) -> tuple[str, list[str]]:
    blocked = seen_urls(state, "video", 14)
    candidates = [c for c in candidates if c.normalized_url() not in blocked]
    today = dt.datetime.now(JST)
    theme = WEEKDAY_THEMES.get(today.weekday(), "AI")
    if not candidates:
        return "🎬 **今日のAI動画**\n\n今日は良い動画が見つからず。明日に期待。", []

    prompt = f"""
あなたは「学生AIスクール」コミュニティ向けの動画キュレーターです。
10〜20分の候補から、今日の曜日テーマに最も合う1本を選び、JSONだけ返してください。

今日: {today.strftime('%Y/%-m/%-d')}({jp_weekday(today)})
テーマ: {theme}
条件:
- 通学中に見られる軽い1本
- 監視対象チャンネル以外は選ばない
- 候補のtitle/descriptionに書かれていない内容を足さない
- 学生の学業・就活・キャリアへの示唆を書く
- 英語タイトルは日本語訳を併記
- 3〜5文
- 矢印絵文字禁止

返却JSON:
{{"channel":"...","duration":"◯分","date":"YYYY/M/D","title":"...","body":"...","url":"..."}}

候補:
{candidates_for_prompt(candidates, 30)}
"""
    try:
        item = extract_json(gemini_generate(prompt))
    except Exception as exc:
        print(f"warning: Gemini daily video failed: {exc}", file=sys.stderr)
        c = candidates[0]
        item = {
            "channel": c.channel or c.source,
            "duration": format_duration(c.duration_seconds),
            "date": slash_date(c.published),
            "title": c.title,
            "body": c.snippet[:180] or "AIを短時間で学べる動画です。学生はテーマの全体像をつかむ入口として使えます。",
            "url": c.url,
        }

    selected = find_candidate_by_url(candidates, item.get("url", ""))
    if selected is None or not selected.duration_seconds or not 600 <= selected.duration_seconds <= 1200:
        selected = candidates[0]
        item = video_item(selected, "今日のAI動画")
        item["duration"] = format_duration(selected.duration_seconds)

    message = "\n".join(
        [
            f"🎬 **今日のAI動画** — {today.strftime('%Y/%-m/%-d')}({jp_weekday(today)})・テーマ：{theme}",
            "",
            f"**{sanitize_inline(item.get('channel', 'YouTube'))}** _(再生時間: {sanitize_inline(item.get('duration', ''))})_ — {sanitize_inline(item.get('date', today.strftime('%Y/%-m/%-d')))}公開",
            f"「{sanitize_inline(item.get('title', ''))}」",
            sanitize_body(item.get("body", "")),
            item.get("url", "").strip(),
            "",
            "---",
            "_土曜の朝には先週まとめ5本配信。今日はサクッと1本どうぞ。_",
        ]
    )
    validate_message(message, expected_emoji="🎬")
    return message, [normalize_url(item.get("url", ""))]


def build_weekly_video_message(candidates: list[Candidate], state: dict[str, Any]) -> tuple[str, list[str]]:
    blocked = seen_urls(state, "video", 28)
    candidates = [c for c in candidates if c.normalized_url() not in blocked]
    today = dt.datetime.now(JST)
    start = today.date() - dt.timedelta(days=7)
    end = today.date() - dt.timedelta(days=1)
    long_candidates = [
        c
        for c in candidates
        if c.duration_seconds
        and c.duration_seconds >= 3600
        and channel_matches(c, ALLOWED_LONG_CHANNELS)
    ]
    short_candidates = [
        c
        for c in candidates
        if c.duration_seconds
        and 600 <= c.duration_seconds <= 1200
        and channel_matches(c, ALLOWED_SHORT_CHANNELS)
    ]
    if not long_candidates or len(short_candidates) < 4:
        return "🎬 **今週のAI動画キャッチアップ**\n\n先週は新着が少なかったので、今週の配信はお休みします。", []

    prompt_candidates = dedupe(long_candidates[:12] + short_candidates[:30])
    supplementing = any(not within_date_range(c.published, start, end) for c in prompt_candidates)
    prompt = f"""
あなたは「学生AIスクール」コミュニティ向けの動画キュレーターです。
候補から長尺1本と短尺4本を選び、JSONだけ返してください。

期間: {start.strftime('%-m/%-d')}〜{end.strftime('%-m/%-d')}
条件:
- 長尺1本: 1時間以上。学生に刺さるAI × キャリア/経済/倫理を優先
- 短尺4本: 10〜20分程度。技術系1、ビジネス系1、社会倫理系1、実用Tips系1
- 監視対象チャンネル以外は選ばない
- 候補のtitle/descriptionに書かれていない内容を足さない
- 英語タイトルは日本語訳を併記
- 架空の内容を足さない

返却JSON:
{{"long":{{"channel":"...","duration":"◯時間◯分","date":"YYYY/M/D","title":"...","body":"4〜6文","url":"..."}},
"shorts":[{{"label":"短尺・技術系","channel":"...","duration":"◯分","date":"YYYY/M/D","title":"...","body":"3〜4文","url":"..."}}]}}

候補:
{candidates_for_prompt(prompt_candidates, 45)}
"""
    try:
        data = extract_json(gemini_generate(prompt))
    except Exception as exc:
        print(f"warning: Gemini weekly video failed: {exc}", file=sys.stderr)
        data = fallback_weekly_video(long_candidates + short_candidates)

    long = data.get("long", {})
    if find_candidate_by_url(long_candidates, long.get("url", "")) is None:
        long = video_item(long_candidates[0], "長尺")
    raw_shorts = data.get("shorts", [])[:4]
    shorts = []
    used_short_urls = set()
    for item in raw_shorts:
        selected = find_candidate_by_url(short_candidates, item.get("url", ""))
        if selected is None or normalize_url(selected.url) in used_short_urls:
            continue
        used_short_urls.add(normalize_url(selected.url))
        shorts.append(item)
    for candidate in short_candidates:
        if len(shorts) >= 4:
            break
        if normalize_url(candidate.url) in used_short_urls:
            continue
        shorts.append(video_item(candidate, ["短尺・技術系", "短尺・ビジネス系", "短尺・社会倫理系", "短尺・実用Tips系"][len(shorts)]))
        used_short_urls.add(normalize_url(candidate.url))
    if len(shorts) < 4:
        return "🎬 **今週のAI動画キャッチアップ**\n\n先週は新着が少なかったので、今週の配信はお休みします。", []
    lines = [
        f"🎬 **今週のAI動画キャッチアップ** — {today.strftime('%Y/%-m')} W{week_of_month(today.date())}（{start.strftime('%-m/%-d')}〜{end.strftime('%-m/%-d')}）",
        "",
    ]
    if supplementing:
        lines.extend(["先週は新着が少なかったので、評価の高い過去動画から補完しています。", ""])
    lines.extend([
        f"**{sanitize_inline(long.get('channel', 'YouTube'))}** _(再生時間: {sanitize_inline(long.get('duration', ''))})_ — {sanitize_inline(long.get('date', today.strftime('%Y/%-m/%-d')))}公開",
        f"「{sanitize_inline(long.get('title', ''))}」",
        sanitize_body(long.get("body", "")),
        long.get("url", "").strip(),
        "",
    ])
    used_urls = [normalize_url(long.get("url", ""))]
    for idx, item in enumerate(shorts, 2):
        label = sanitize_inline(item.get("label", f"{idx}本目"))
        lines.extend(
            [
                f"**[{idx}本目: {label}] {sanitize_inline(item.get('channel', 'YouTube'))}** _({sanitize_inline(item.get('duration', ''))})_ — {sanitize_inline(item.get('date', today.strftime('%Y/%-m/%-d')))}公開",
                f"「{sanitize_inline(item.get('title', ''))}」",
                sanitize_body(item.get("body", "")),
                item.get("url", "").strip(),
                "",
            ]
        )
        used_urls.append(normalize_url(item.get("url", "")))
    lines.extend(["---", "_平日は毎朝7:00に1本配信、土曜はこのまとめ。来週もどうぞ。_"])
    message = "\n".join(lines)
    validate_message(message, expected_emoji="🎬")
    return message, used_urls


def fallback_weekly_video(candidates: list[Candidate]) -> dict[str, Any]:
    long_candidates = [c for c in candidates if c.duration_seconds and c.duration_seconds >= 3600]
    short_candidates = [c for c in candidates if c.duration_seconds and 600 <= c.duration_seconds <= 1200]
    long = long_candidates[0] if long_candidates else candidates[0]
    shorts = short_candidates[:4]
    return {
        "long": video_item(long, "長尺"),
        "shorts": [video_item(c, label) for c, label in zip(shorts, ["短尺・技術系", "短尺・ビジネス系", "短尺・社会倫理系", "短尺・実用Tips系"])],
    }


def channel_matches(candidate: Candidate, names: list[str]) -> bool:
    if names == ALLOWED_LONG_CHANNELS:
        return candidate.channel_id in LONG_SOURCE_CHANNEL_IDS
    if names == ALLOWED_SHORT_CHANNELS:
        return candidate.channel_id in SHORT_SOURCE_CHANNEL_IDS
    channel = (candidate.channel or candidate.source or "").casefold()
    return any(name.casefold() in channel for name in names)


def find_candidate_by_url(candidates: list[Candidate], url: str) -> Candidate | None:
    normalized = normalize_url(url)
    if not normalized:
        return None
    for candidate in candidates:
        if candidate.normalized_url() == normalized:
            return candidate
    return None


def within_date_range(value: str, start: dt.date, end: dt.date) -> bool:
    parsed = parse_date(value)
    if not parsed:
        return False
    try:
        day = dt.date.fromisoformat(parsed)
    except ValueError:
        return False
    return start <= day <= end


def has_weekly_minimum(candidates: list[Candidate]) -> bool:
    long_candidates = [
        c
        for c in candidates
        if c.duration_seconds
        and c.duration_seconds >= 3600
        and channel_matches(c, ALLOWED_LONG_CHANNELS)
    ]
    short_candidates = [
        c
        for c in candidates
        if c.duration_seconds
        and 600 <= c.duration_seconds <= 1200
        and channel_matches(c, ALLOWED_SHORT_CHANNELS)
    ]
    return bool(long_candidates) and len(short_candidates) >= 4


def video_item(candidate: Candidate, label: str) -> dict[str, str]:
    return {
        "label": label,
        "channel": candidate.channel or candidate.source,
        "duration": format_duration(candidate.duration_seconds),
        "date": slash_date(candidate.published),
        "title": candidate.title,
        "body": candidate.snippet[:220] or "AIの最新トピックを学べる動画です。学生は領域の見取り図をつかむ入口として活用できます。",
        "url": candidate.url,
    }


def normalize_url(url: str) -> str:
    return Candidate(title="x", url=url, source="x").normalized_url()


def sanitize_inline(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def sanitize_body(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text.replace("👉", "")


def slash_date(value: str) -> str:
    parsed = parse_date(value)
    if not parsed:
        return dt.datetime.now(JST).strftime("%Y/%-m/%-d")
    y, m, d = parsed.split("-")
    return f"{int(y)}/{int(m)}/{int(d)}"


def jp_weekday(value: dt.datetime) -> str:
    return "月火水木金土日"[value.weekday()]


def week_of_month(day: dt.date) -> int:
    return (day.day - 1) // 7 + 1


def format_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    minutes = round(seconds / 60)
    if minutes >= 60:
        return f"{minutes // 60}時間{minutes % 60}分"
    return f"{minutes}分"


def validate_message(message: str, *, expected_emoji: str) -> None:
    if "👉" in message or ":point_right:" in message:
        raise ValueError("forbidden arrow emoji found")
    if not message.startswith(expected_emoji):
        raise ValueError(f"message must start with {expected_emoji}")
    emoji_count = sum(1 for char in message if char in {"📰", "🎬"})
    if emoji_count != 1:
        raise ValueError("message must contain exactly one leading emoji")
    if len(message) > 5000:
        raise ValueError("Slack message exceeds 5000 characters")
    if re.search(r"https?://", message) is None:
        raise ValueError("message has no URL")


def post_slack(message: str, webhook_env: str, *, dry_run: bool) -> None:
    if dry_run:
        print(message)
        return
    webhook = os.getenv(webhook_env)
    if not webhook:
        raise RuntimeError(f"{webhook_env} is not set")
    payload = {"text": to_slack_mrkdwn(message)}
    response = request_raw(webhook, method="POST", data=payload)
    if response.strip().lower() != "ok":
        raise RuntimeError(f"Unexpected Slack webhook response: {response[:200]}")


def to_slack_mrkdwn(message: str) -> str:
    """Convert the canonical Markdown template into Slack mrkdwn."""
    converted = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<\2|\1>", message)
    converted = re.sub(r"\*\*([^*]+)\*\*", r"*\1*", converted)
    return converted


def run(mode: str, *, dry_run: bool) -> None:
    state = load_state()
    clean_state(state)
    if mode == "news":
        candidates = collect_news_candidates(days=2)
        message, urls = build_news_message(candidates, state)
        post_slack(message, NEWS_WEBHOOK_ENV, dry_run=dry_run)
        if not dry_run:
            mark_seen(state, "news", urls)
    elif mode == "daily-video":
        candidates = collect_video_candidates(weekly=False)
        message, urls = build_daily_video_message(candidates, state)
        post_slack(message, VIDEO_WEBHOOK_ENV, dry_run=dry_run)
        if not dry_run:
            mark_seen(state, "video", urls)
    elif mode == "weekly-video":
        candidates = collect_video_candidates(weekly=True, days=7)
        if not has_weekly_minimum(candidates):
            candidates = collect_video_candidates(weekly=True, days=45)
        message, urls = build_weekly_video_message(candidates, state)
        post_slack(message, VIDEO_WEBHOOK_ENV, dry_run=dry_run)
        if not dry_run:
            mark_seen(state, "video", urls)
    else:
        raise ValueError(f"unknown mode: {mode}")
    if not dry_run:
        save_state(state)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["news", "daily-video", "weekly-video"], required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(args.mode, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
