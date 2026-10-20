#!/usr/bin/env python3
"""Fetch hot news (AI / trade / economy / world) from RSS and push it to Telegram.

Pure standard library, no third-party packages. Runs anywhere:
  * GitHub Actions (see .github/workflows/news.yml)  <- recommended, 24/7
  * any VPS with cron / systemd timer
  * this sandbox (manual runs)

Usage:
  export TELEGRAM_BOT_TOKEN=123456:ABC...
  python3 scripts/news_push.py --chat-id 123456789
  python3 scripts/news_push.py --chat-id 123456789 --dry-run
  python3 scripts/news_push.py --chat-id 123456789 --max-age 180 --max-items 4
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

API = "https://api.telegram.org/bot{token}/{method}"
USER_AGENT = "Mozilla/5.0 (compatible; ClineNewsBot/1.0; +https://github.com/)"
BEIJING = timezone(timedelta(hours=8))
MAX_MESSAGE_CHARS = 3500
STATE_HISTORY = 200

# 每个类别一组 Google News 查询(zh-CN),覆盖不同关键词避免漏新闻
CATEGORIES: List[Tuple[str, List[str]]] = [
    ("AI", ["AI 大模型", "OpenAI 英伟达", "人工智能 监管"]),
    ("外贸", ["关税 出口", "外贸 海关", "航运 运价"]),
    ("经济", ["美联储 利率", "央行 降息", "CPI PMI 数据"]),
    ("国际", ["地缘 冲突", "中东 局势", "联合国 制裁"]),
]

# 额外的直连 RSS(BBC / 36氪),补充英文与科技深度报道
EXTRA_FEEDS: List[Tuple[str, str]] = [
    ("AI", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
    ("经济", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("国际", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("AI", "https://36kr.com/feed"),
]

GOOGLE_NEWS = (
    "https://news.google.com/rss/search?q={query}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
)


class NewsError(RuntimeError):
    """Raised for unrecoverable problems (bad token, config, ...)."""


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def http_get(url: str, timeout: int = 20) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def strip_tags(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_feed(payload: bytes) -> List[Dict[str, Any]]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        log(f"skip unparsable feed: {exc}")
        return []

    items: List[Dict[str, Any]] = []
    for node in root.findall(".//item"):
        title = strip_tags(node.findtext("title") or "")
        link = (node.findtext("link") or "").strip()
        if not title or not link:
            continue
        published: Optional[datetime] = None
        raw_date = node.findtext("pubDate")
        if raw_date:
            try:
                published = parsedate_to_datetime(raw_date)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                published = None
        items.append(
            {
                "title": title,
                "link": link,
                "summary": strip_tags(
                    node.findtext("description") or node.findtext("summary") or ""
                ),
                "published": published,
            }
        )
    return items


def collect(
    max_age_minutes: int,
) -> Tuple[Dict[str, List[Dict[str, Any]]], int]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
    buckets: Dict[str, List[Dict[str, Any]]] = {tag: [] for tag, _ in CATEGORIES}
    failures = 0

    for tag, queries in CATEGORIES:
        for query in queries:
            url = GOOGLE_NEWS.format(query=urllib.parse.quote(query))
            try:
                payload = http_get(url)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                log(f"[{tag}] feed error {query}: {exc}")
                failures += 1
                continue
            for item in parse_feed(payload):
                item["category"] = tag
                item["source"] = (item["title"].split(" - ")[-1] or "Google News").strip()
                if item["published"] and item["published"] < cutoff:
                    continue
                buckets[tag].append(item)

    for tag, feed in EXTRA_FEEDS:
        try:
            payload = http_get(feed)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log(f"[{tag}] feed error {feed}: {exc}")
            failures += 1
            continue
        host = urllib.parse.urlparse(feed).netloc
        for item in parse_feed(payload):
            item["category"] = tag
            item["source"] = host
            if item["published"] and item["published"] < cutoff:
                continue
            buckets[tag].append(item)

    return buckets, failures


def normalize(title: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", title).lower()


def select(
    buckets: Dict[str, List[Dict[str, Any]]],
    state: Dict[str, Any],
    max_items: int,
) -> List[Dict[str, Any]]:
    seen_urls = set(state.get("urls", []))
    seen_titles = set(state.get("titles", []))
    picked: List[Dict[str, Any]] = []
    used_titles = set()

    for tag, _ in CATEGORIES:
        candidates = [
            item
            for item in buckets.get(tag, [])
            if item["link"] not in seen_urls
            and normalize(item["title"]) not in seen_titles
            and normalize(item["title"]) not in used_titles
        ]
        candidates.sort(
            key=lambda item: item["published"]
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        if not candidates:
            continue
        best = candidates[0]
        used_titles.add(normalize(best["title"]))
        picked.append(best)
        if len(picked) >= max_items:
            break
    return picked


def build_message(items: List[Dict[str, Any]], now: Optional[datetime] = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).astimezone(BEIJING).strftime(
        "%Y-%m-%d %H:%M"
    )
    lines = [
        "<b>🔥 要闻速递 · AI / 外贸 / 经济 / 国际</b>",
        f"<i>北京时间 {stamp}</i>",
        "",
    ]
    for index, item in enumerate(items, start=1):
        summary = item["summary"][:160].strip()
        if normalize(item["title"]) in normalize(summary):
            summary = ""
        lines.append(
            f"<b>{index}. [{item['category']}] {html.escape(item['title'])}</b>"
        )
        if summary:
            lines.append(html.escape(summary))
        lines.append(
            f'<a href="{html.escape(item["link"], quote=True)}">'
            f"{html.escape(item['source'])}</a>"
        )
        lines.append("")
    message = "\n".join(lines).strip()
    if len(message) > MAX_MESSAGE_CHARS:
        message = message[: MAX_MESSAGE_CHARS - 20].rstrip() + "\n…"
    return message


def send_message(token: str, chat_id: str, text: str) -> Dict[str, Any]:
    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode()
    request = urllib.request.Request(
        API.format(token=token, method="sendMessage"), data=data, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise NewsError(f"Telegram HTTP {exc.code}: {raw[:300]}") from exc
    except urllib.error.URLError as exc:
        raise NewsError(f"Telegram network error: {exc.reason}") from exc
    if not payload.get("ok"):
        raise NewsError(f"Telegram error: {payload.get('description')}")
    return payload["result"]


def load_state(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {"urls": [], "titles": [], "empty_runs": 0, "last_push": None}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        log(f"state unreadable ({exc}), starting fresh")
        return {"urls": [], "titles": [], "empty_runs": 0, "last_push": None}
    state.setdefault("urls", [])
    state.setdefault("titles", [])
    state.setdefault("empty_runs", 0)
    state.setdefault("last_push", None)
    return state


def save_state(path: str, state: Dict[str, Any]) -> None:
    state["urls"] = state["urls"][-STATE_HISTORY:]
    state["titles"] = state["titles"][-STATE_HISTORY:]
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def run(args: argparse.Namespace) -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not args.dry_run and not token:
        raise NewsError("TELEGRAM_BOT_TOKEN is not set")

    state = load_state(args.state)
    buckets, failures = collect(args.max_age)
    if failures and not any(buckets.values()):
        raise NewsError(f"all feeds failed ({failures} errors)")

    items = select(buckets, state, args.max_items)
    if not items:
        state["empty_runs"] = int(state.get("empty_runs", 0)) + 1
        if state["empty_runs"] >= args.heartbeat_after:
            text = (
                "<b>💤 心跳</b>\n过去一段时间四类均无新增重要新闻，任务运行正常。"
            )
            log("heartbeat due")
            if not args.dry_run:
                send_message(token, args.chat_id, text)
            state["empty_runs"] = 0
            state["last_push"] = datetime.now(timezone.utc).isoformat()
            save_state(args.state, state)
            return 0
        log(f"no new items (empty_runs={state['empty_runs']})")
        if not args.dry_run:
            save_state(args.state, state)
        return 0

    message = build_message(items)
    if args.dry_run:
        print(message)
        print(f"\n# dry-run: {len(items)} item(s) would be sent", file=sys.stderr)
        return 0

    result = send_message(token, args.chat_id, message)
    log(f"sent message_id={result.get('message_id')} items={len(items)}")

    state["urls"].extend(item["link"] for item in items)
    state["titles"].extend(normalize(item["title"]) for item in items)
    state["empty_runs"] = 0
    state["last_push"] = datetime.now(timezone.utc).isoformat()
    save_state(args.state, state)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--chat-id", required=True, help="Telegram chat id")
    parser.add_argument("--state", default=".news_seen.json", help="Dedupe state file")
    parser.add_argument(
        "--max-age", type=int, default=240, help="Max item age in minutes"
    )
    parser.add_argument("--max-items", type=int, default=4, help="Items per message")
    parser.add_argument(
        "--heartbeat-after",
        type=int,
        default=6,
        help="Send a heartbeat after N empty runs",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print instead of sending"
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except NewsError as exc:
        log(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

