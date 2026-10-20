#!/usr/bin/env python3
"""Fetch hot news (AI / 外贸 / 经济 / 国际) and push a filtered digest to Telegram.

Pure standard library, no third-party packages.

Design notes:
  * Several Chinese sources are polled in parallel (Google News RSS, 华尔街见闻,
    新浪财经 7x24, 36氪) plus a few English feeds as fallback.
  * Every candidate is scored so that "substantive" stories (policy decisions,
    data surprises, price/shipping moves, escalation) outrank commentary,
    round-ups and market noise.
  * The publisher URL behind a Google News redirect is decoded, then the article
    page is fetched once to build a real 1-2 sentence summary.
  * At most one item per category, at most 4 items, and a digest is only sent
    when at least two categories have something new.

Usage:
  export TELEGRAM_BOT_TOKEN=123456:ABC...
  python3 scripts/news_push.py --chat-id 123456789
  python3 scripts/news_push.py --chat-id 123456789 --dry-run
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

BEIJING = timezone(timedelta(hours=8))
MAX_MESSAGE_CHARS = 3500
STATE_HISTORY = 200
# 低于该分数的条目视为无实质进展(展会稿、纯解读、行情快照等),整类跳过而不硬凑。
MIN_SCORE = 4
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
GOOGLE_NEWS = (
    "https://news.google.com/rss/search?q={query}+when%3A2d"
    "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
)
TELEGRAM_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "telegram.py"
)

CATEGORIES: List[Tuple[str, List[str]]] = [
    (
        "AI",
        [
            "AI 大模型 发布",
            "英伟达 芯片 算力",
            "人工智能 监管 政策",
            "OpenAI Anthropic 融资",
        ],
    ),
    (
        "外贸",
        [
            "关税 贸易 政策",
            "出口 海关 数据",
            "航运 运价 港口",
            "跨境电商 供应链",
            "商务部 贸易 摩擦",
            "港口 拥堵 航运",
        ],
    ),
    (
        "经济",
        [
            "央行 利率 降息",
            "CPI PPI 经济数据",
            "GDP 统计局 增长",
            "美联储 通胀",
        ],
    ),
    (
        "国际",
        [
            "地缘 冲突 局势",
            "制裁 外交 谈判",
            "中东 停火",
            "油轮 霍尔木兹",
            "选举 政局",
            "俄乌 冲突",
            "联合国 制裁 决议",
        ],
    ),
]

# (category hint, url). Direct feeds keep their real URLs, no redirect decoding.
DIRECT_FEEDS: List[Tuple[str, str]] = [
    ("AI", "https://36kr.com/feed"),
    ("AI", "https://techcrunch.com/feed/"),
    ("AI", "https://www.theverge.com/rss/index.xml"),
    ("经济", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("国际", "https://feeds.bbci.co.uk/news/world/rss.xml"),
]

# JSON endpoints of Chinese live-wire services: fast, real links, usable text.
LIVE_ENDPOINTS: List[Tuple[str, str]] = [
    (
        "华尔街见闻",
        "https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&limit=40",
    ),
    (
        "新浪财经",
        "https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=40&zhibo_id=152&tag_id=0",
    ),
]

# Keywords per category, used to route live-wire items and to double-check feeds.
CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "AI": [
        "AI", "人工智能", "大模型", "模型", "算力", "芯片", "英伟达", "台积电",
        "OpenAI", "Anthropic", "DeepMind", "Gemini", "ChatGPT", "GPU", "HBM",
        "数据中心", "智能体", "开源模型", "机器人",
    ],
    "外贸": [
        "关税", "贸易", "出口", "进口", "海关", "外贸", "航运", "运价", "港口",
        "集装箱", "跨境电商", "供应链", "反倾销", "制裁清单", "东南亚", "出海",
        "SCFI", "货运", "班轮", "保税", "自贸",
    ],
    "经济": [
        "央行", "利率", "降息", "加息", "LPR", "CPI", "PPI", "PMI", "GDP",
        "通胀", "就业", "失业率", "财政", "国债", "汇率", "人民币", "美联储",
        "统计局", "货币政策", "债市", "股市",
    ],
    "国际": [
        "冲突", "袭击", "停火", "谈判", "峰会", "外交", "制裁", "选举", "政局",
        "中东", "俄乌", "伊朗", "以色列", "联合国", "北约", "能源安全", "政变",
        "导弹", "边境",
    ],
}

POSITIVE = {
    "发布": 3, "推出": 2, "上调": 3, "下调": 3, "降息": 4, "加息": 4,
    "加征": 4, "关税": 3, "制裁": 4, "出口": 2, "进口": 2, "运价": 4,
    "涨价": 3, "暴跌": 3, "大涨": 3, "签署": 3, "达成": 3, "批准": 3,
    "禁令": 3, "裁员": 3, "融资": 3, "收购": 3, "IPO": 2, "突破": 2,
    "GDP": 4, "CPI": 4, "PMI": 4, "失业率": 3, "冲突": 4, "袭击": 4,
    "峰会": 3, "停火": 4, "谈判": 2, "监管": 2, "芯片": 2, "算力": 2,
    "大模型": 2, "反倾销": 4, "港口": 2, "央行": 2, "利率": 2, "财报": 2,
    "商务部": 3, "海关总署": 3, "创历史": 3, "新高": 2, "新低": 2,
    "制裁": 4, "谈判": 2, "联合声明": 3, "协议": 2,
}

NEGATIVE = {
    "小幅": -3, "震荡": -3, "收盘": -2, "观点": -3, "分析": -2, "前瞻": -3,
    "盘点": -3, "预测": -2, "汇总": -5, "提醒": -5, "重点关注": -5,
    "早知道": -5, "花絮": -5, "八卦": -5, "明星": -5, "娱乐": -5,
    "天气": -5, "寒潮": -5, "彩票": -6, "旅游": -4, "美食": -5, "直播": -2,
    "球赛": -4, "股价异动": -3, "传闻": -2, "据悉": -1, "研报": -3,
    "专家": -2, "解读": -2, "涨停": -4, "半日": -4, "收评": -5,
    "调查与思考": -4, "如何": -2, "这招": -4, "大笑": -5, "回旋镖": -4,
    # 纯表态 / 观点 / 二手解读,优先级低于政策落地与数据
    "解读": -3, "评论": -4, "专访": -6, "一文": -4, "全文": -4,
    "梳理": -5, "速览": -5, "他指出": -5, "他表示": -5, "她说": -5,
    "他说": -5, "官员称": -4, "回应称": -3, "发言人称": -2,
    # 分析师/专家观点不是事实进展
    "专家称": -6, "分析师": -4, "首席": -4, "认为": -3, "预计": -3,
    # 前官员/机构的个人预测属于观点,不是政策或数据本身
    "前副主席": -5, "大概率": -4, "推手": -5,
    "划等号": -4, "前景": -4, "展望": -4, "或将": -3,
    # 标题党 / 自媒体评论
    "遮羞布": -8, "底牌": -5, "撕了": -7, "慌了": -6, "崩了": -5,
    "炸了": -6, "竟然": -6, "真相": -5, "内幕": -6, "意味着什么": -4,
    "一文读懂": -6, "大白话": -5, "重磅信号": -3, "释放信号": -3,
    # 疑问式标题多为解读/评论,信息密度低
    "怎么看": -5, "如何看": -5, "怎么走": -4, "何去何从": -4,
    # 特稿/软文式开头,信息密度低
    "文 |": -4, "作者": -3, "编辑": -2, "本文": -4, "叙事": -5, "赛道": -3,
    # 全角竖线的署名/配图注,是人物特稿而非事件
    "文｜": -6, "编辑｜": -5, "作者｜": -5, "图源": -4,
    "故事": -4, "报告": -4, "图解": -5, "白皮书": -4, "榜单": -5,
    "排行": -5, "走低": -3, "跌超": -3, "涨超": -3, "月访问量": -3,
    "合作探索": -4, "非约束性": -3, "谅解备忘录": -3, "战略签约": -4,
    # 展会/招商/启动会一类活动稿,不是可推送的实质进展
    "展会": -6, "盛会": -6, "启幕": -5, "揭牌": -5, "启动仪式": -6,
    "推介会": -6, "对接会": -5, "战略合作": -4, "座谈": -4, "招商": -4,
    # 工商注册/注册资本类快讯没有行业影响,不算热点
    "注册资本": -8, "天眼查": -8, "企查查": -8, "经营范围": -6, "全资持股": -6,
    # 厂商获奖/评级软文
    "五星": -8, "获评": -6, "获奖": -6, "荣获": -6, "榜单": -5,
    "加速器": -3, "训练营": -5, "大赛": -4, "评选": -4,
    # 论坛/发布会只宣布意向,缺少可验证的落地动作
    "论坛": -4, "开幕式": -4, "发布会": -2, "圆桌": -6, "年会": -5, "致辞": -6,
    # 数据前瞻/预告稿:写的是将要发生的事,不是已落地的结果
    "揭晓": -4, "市场参考": -5, "本周晚些": -4, "或打乱": -3,
    # 明确「符合预期/按兵不动」的结果没有超预期冲击,不算热点
    "符合预期": -4, "符合市场预期": -4, "按兵不动": -2, "皆有可能": -2,
}

# 信源质量:主流通讯社/官方/专业媒体加分,内容农场与自媒体降权。
TRUSTED_HOSTS = (
    "reuters.com", "apnews.com", "bbc.co.uk", "bbc.com", "ft.com", "wsj.com",
    "bloomberg.com", "xinhuanet.com", "news.cn", "chinanews.com.cn",
    "stats.gov.cn", "pbc.gov.cn", "mofcom.gov.cn", "customs.gov.cn",
    "caixin.com", "yicai.com", "cls.cn", "stcn.com", "thepaper.cn",
    "wallstreetcn.com", "36kr.com", "techcrunch.com", "theverge.com",
    "ithome.com", "jiqizhixin.com", "qbitai.com", "newscientist.com",
)
LOW_TRUST_HOSTS = (
    "sohu.com", "baijiahao.baidu.com", "toutiao.com", "zhihu.com", "weibo.com",
    "k.sina.com.cn", "dy.163.com", "mp.weixin.qq.com", "hao.360.com",
    "chineseherald.co.nz", "chinesenzherald.co.nz",
    # 机器翻译/转载聚合站,常混入「WhatsApp频道」一类抓取残留
    "idnfinancials.com", "stockstar.com",
    # 无编辑品牌的自建站/博客,以解读、观点类长文为主
    "or100.cc",
    # 零售交易资讯站,正文多为行情点评与免责声明
    "fx678.com",
    # 境外中文评论站,以立场性评论/连线节目为主,缺一手事实
    "ntdtv.com", "epochtimes.com", "secretchina.com", "aboluowang.com",
    # 汽车门户的用户投稿号,多为厂商通稿
    "aikahao.xcar.com.cn", "xcar.com.cn",
)


def source_weight(link: str = "", media: str = "") -> int:
    host = urllib.parse.urlparse(link).netloc.lower()
    label = (media or "").lower()
    if any(host == h or host.endswith("." + h) for h in TRUSTED_HOSTS):
        return 3
    if any(host == h or host.endswith("." + h) for h in LOW_TRUST_HOSTS):
        return -5
    if any(name in label for name in TRUSTED_MEDIA):
        return 3
    if any(name in label for name in LOW_TRUST_MEDIA):
        return -5
    return 0


TRUSTED_MEDIA = (
    "reuters", "路透", "美联社", "bbc", "新华社", "新华网", "人民日报",
    "央视", "中国新闻网", "财新", "第一财经", "财联社", "澎湃", "界面",
    "华尔街见闻", "36氪", "techcrunch", "the verge", "bloomberg", "彭博",
    "金融时报", "华尔街日报", "cnbc", "联合早报", "日经",
)
LOW_TRUST_MEDIA = (
    "搜狐", "网易", "百家号", "头条", "微信公众号", "知乎", "微博", "大鱼号",
    "企鹅号", "一点资讯", "新唐人", "大纪元", "阿波罗网",
)

NUMBER_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:%|亿|万|美元|元|吨|标箱|点)")


STATS = {"ok": 0, "fail": 0}


class NewsError(RuntimeError):
    """Raised for unrecoverable problems (bad token, config, ...)."""


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def http_get(url: str, timeout: int = 25) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def decode_page(raw: bytes) -> str:
    """Decode HTML bytes using the declared charset (many CN sites are GBK)."""
    head = raw[:2048].decode("ascii", "ignore")
    match = re.search(r'charset=["\']?([\w-]+)', head, re.I)
    candidates = [match.group(1)] if match else []
    candidates += ["utf-8", "gb18030"]
    for charset in candidates:
        try:
            return raw.decode(charset)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "ignore")


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
                "via": "rss",
            }
        )
    return items


def collect_feed(source: str, url: str) -> List[Dict[str, Any]]:
    try:
        payload = http_get(url)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log(f"feed error [{source}] {url}: {exc}")
        STATS["fail"] += 1
        return []
    items = parse_feed(payload)
    STATS["ok"] += 1
    log(f"feed ok [{source}] {len(items)} items")
    return items


def collect_google_news(query: str) -> List[Dict[str, Any]]:
    url = GOOGLE_NEWS.format(query=urllib.parse.quote(query))
    try:
        payload = http_get(url)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log(f"feed error [google:{query}]: {exc}")
        STATS["fail"] += 1
        return []
    items = parse_feed(payload)
    STATS["ok"] += 1
    log(f"feed ok [google:{query}] {len(items)} items")
    for item in items:
        # "标题 - 媒体名" -> title + source
        parts = item["title"].rsplit(" - ", 1)
        if len(parts) == 2 and len(parts[1]) <= 24:
            item["title"] = parts[0].strip()
            item["media"] = parts[1].strip()
        else:
            item["media"] = "Google News"
        item["via"] = "gnews"
    return items


def headline(text: str, limit: int = 60) -> str:
    """First sentence of a live-wire item, hard-cut on a clause boundary."""
    text = re.sub(r"\s+", " ", text or "").strip()
    first = re.split(r"(?<=[。！？!?])", text)[0]
    if len(first) <= limit:
        return first
    cut = text[:limit]
    for mark in ("，", "；", "、", ",", " "):
        pos = cut.rfind(mark)
        if pos >= limit // 2:
            return cut[:pos]
    return cut


def collect_live(name: str, url: str, limit: int = 40) -> List[Dict[str, Any]]:
    try:
        payload = http_get(url)
        data = json.loads(payload.decode("utf-8", "ignore"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log(f"live error [{name}] {url}: {exc}")
        STATS["fail"] += 1
        return []

    items: List[Dict[str, Any]] = []
    if name == "华尔街见闻":
        for node in data.get("data", {}).get("items", [])[:limit]:
            text = (node.get("content_text") or "").strip()
            title = (node.get("title") or "").strip()
            link = node.get("uri") or ""
            stamp = node.get("display_time")
            if not text or not link:
                continue
            items.append(
                {
                    "title": title or headline(text),
                    "link": link,
                    "summary": text,
                    "published": (
                        datetime.fromtimestamp(int(stamp), tz=timezone.utc)
                        if stamp
                        else None
                    ),
                    "media": "华尔街见闻",
                    "via": "live",
                }
            )
    else:  # 新浪财经 7x24
        feed = data.get("result", {}).get("data", {}).get("feed", {}).get("list", [])
        for node in feed[:limit]:
            text = strip_tags(node.get("rich_text") or "")
            link = node.get("docurl") or ""
            if not text or not link:
                continue
            items.append(
                {
                    "title": text[:80],
                    "link": link,
                    "summary": text,
                    "published": None,
                    "media": "新浪财经",
                    "via": "live",
                }
            )
    STATS["ok"] += 1
    log(f"live ok [{name}] {len(items)} items")
    return items



def _meta_description(page: str) -> str:
    for match in re.finditer(r"<meta\b[^>]*>", page, re.I):
        tag = match.group(0)
        if not re.search(
            r'(?:property|name)=["\'](?:og:description|description|twitter:description)["\']',
            tag,
            re.I,
        ):
            continue
        content = re.search(r'content=["\']([^"\']*)["\']', tag, re.I)
        if content:
            text = strip_tags(content.group(1))
            if len(text) >= 20:
                return text
    return ""


def resolve_google_news(url: str) -> str:
    """Decode a news.google.com redirect into the publisher URL (best effort)."""
    try:
        page = decode_page(http_get(url, timeout=15))
    except (urllib.error.URLError, TimeoutError, OSError):
        return url
    signature = re.search(r'data-n-a-sg="([^"]+)"', page)
    timestamp = re.search(r'data-n-a-ts="([^"]+)"', page)
    article_id = re.search(r'data-n-a-id="([^"]+)"', page)
    if not (signature and timestamp and article_id):
        return url

    inner = (
        '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,'
        'null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
        f'"{article_id.group(1)}",{timestamp.group(1)},"{signature.group(1)}"]'
    )
    body = "f.req=" + urllib.parse.quote(json.dumps([[['Fbv4je', inner]]]))
    request = urllib.request.Request(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        data=body.encode(),
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8", "ignore")
        parsed = json.loads(raw.split("\n\n")[1])[:-2]
        real = json.loads(parsed[0][2])[1]
        if isinstance(real, str) and real.startswith("http"):
            return real
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        IndexError,
        KeyError,
    ):
        pass
    return url


def _body_text(page: str, limit: int = 240) -> str:
    """First meaningful paragraphs of an article page, as a fallback summary."""
    page = re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S | re.I)
    parts: List[str] = []
    total = 0
    for raw in re.findall(r"<p[^>]*>(.*?)</p>", page, re.S | re.I):
        text = re.sub(r"\s+", " ", strip_tags(raw)).strip()
        if len(text) < 25:
            continue
        if any(bad in text for bad in NOISE_PARAGRAPH):
            continue
        # 过滤脚本残留/图片地址一类非正文段落
        if re.search(r"[{}<>]|https?://|\.jpg|\.png|var\s|function\s|=\s*\[", text):
            continue
        if len(re.findall(r"[\u4e00-\u9fff]", text)) < 15:
            continue
        parts.append(text)
        total += len(text)
        if total >= limit:
            break
    return " ".join(parts)[:limit]


NOISE_PARAGRAPH = (
    "责任编辑", "免责声明", "版权", "扫码", "关注我们", "原标题", "点击查看",
    "未经授权", "更多精彩", "下载", "客户端",
)


def enrich(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Resolve the publisher URL and pull a usable summary from the page."""
    link = item["link"]
    if item.get("via") == "gnews" or "news.google.com" in link:
        link = resolve_google_news(link)
        if "news.google.com" in link:
            log(f"cannot resolve publisher url: {item['title'][:40]}")
            return None
    item["link"] = canonical_url(link)

    summary = re.sub(r"\s+", " ", item.get("summary") or "").strip()
    summary = clean_summary(summary, item["title"])

    # 摘要太短或被源站用 "..." 截断时,回源抓 meta description,必要时再取正文首段
    truncated = len(summary) < 120 and ("..." in summary or "…" in summary)
    if (len(summary) < 30 or truncated) and link.startswith("http"):
        page = ""
        try:
            page = decode_page(http_get(link, timeout=12))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log(f"summary fetch failed {link}: {exc}")
        if page:
            meta = _meta_description(page)
            if meta and len(meta) > len(summary):
                summary = meta
            if len(summary) < 90 or "..." in summary:
                body = _body_text(page)
                if len(body) > len(summary):
                    summary = body

    summary = clean_summary(summary, item["title"])
    if len(summary) < 20:
        # 快讯站(华尔街见闻 livenews 等)是 JS 外壳,meta/正文都抓不到。
        # 若 feed 原文本身已是完整的事实陈述,直接采用,避免丢掉重要突发。
        raw = OUTLET_TAIL.sub("", re.sub(r"\s+", " ", item.get("summary") or "").strip())
        if len(raw) >= 20 and re.search(r"[。！？!?]", raw):
            summary = raw
    item["summary"] = summary
    if len(summary) < 20:
        return None
    return item


OUTLET_TAIL = re.compile(
    r"(?:[\s\-_|·—]+(?:ThePaper\.cn|cnBeta\.COM|澎湃新闻|新浪财经|新浪网|搜狐|"
    r"网易|腾讯新闻|凤凰网|界面新闻|第一财经|财联社|华尔街见闻|36氪|"
    r"Reuters|Bloomberg|BBC|AP|CNBC|TechCrunch|The Verge|SCMP)\s*)+$",
    re.I,
)
LEAD_MARKER = re.compile(r"^(?:解读|分析|观察|深度|焦点|专访|评论)\s*[｜|:：\-]\s*")
# 快讯页抓下来的正文常带一排「夜间 评论 收藏 分享:」导航按钮,先剥掉。
NAV_PREFIX = re.compile(
    r"^(?:(?:夜间|早间|午间|盘中|实时|快讯|要闻|热点|推荐)\s*)?"
    r"(?:(?:评论|收藏|分享|打印|字号|点赞|举报|返回|更多)\s*[：:]?\s*){2,}[：:]?\s*"
)
# 导航栏/版权声明一类的抓取残留,不能当摘要用。
BOILERPLATE = (
    "广告服务", "联系我们", "隐私政策", "使用条款", "版权所有", "扫码关注",
    "只作为参考", "投资决定", "不构成投资建议", "任何在本网站",
)
# 机翻站点会在正文前挂频道推广,术语表用「[[」引出,均不属于新闻内容。
JUNK_PREFIX = re.compile(
    r"^(?:(?:WhatsApp|Telegram|Twitter|LINE|微信|推特)\s*(?:频道|群|公众号)?\s*"
    r"[|｜:：\-—]?\s*)+",
    re.I,
)
GLOSSARY = re.compile(r"\s*\[\[.*$", re.S)


def is_boilerplate(summary: str) -> bool:
    if sum(summary.count(word) for word in BOILERPLATE) >= 2:
        return True
    if "首页" in summary and "搜索" in summary:
        return True
    # 相关性推荐把几条无关标题拼在一起,常出现多个问号
    if summary.count("？") >= 2:
        return True
    return summary.count("×") >= 2


def _strip_title_prefix(summary: str, nt: str, title: str) -> str:
    """按字符对齐剥掉标题前缀,容忍标题后多挂的栏目名或标点差异。"""
    matched = 0
    cut = 0
    for idx, ch in enumerate(summary):
        low = ch.lower()
        if not re.match(r"[0-9a-z\u4e00-\u9fff]", low):
            continue
        if matched < len(nt) and low == nt[matched]:
            matched += 1
            cut = idx + 1
        else:
            break
    if matched >= int(len(nt) * 0.8):
        return summary[cut:]
    return summary[len(title):]


def clean_summary(summary: str, title: str) -> str:
    """Drop outlet suffixes and lead markers; blank out title-echo summaries."""
    summary = LEAD_MARKER.sub("", re.sub(r"\s+", " ", summary or "").strip())
    summary = NAV_PREFIX.sub("", summary)
    summary = JUNK_PREFIX.sub("", summary)
    summary = GLOSSARY.sub("", summary)
    if is_boilerplate(summary):
        return ""
    # 去掉快讯的「【标题】」外壳,避免按标题长度切前缀时残留一个「】」
    summary = re.sub(r"^【([^】]{8,80})】\s*", r"\1 ", summary)
    summary = OUTLET_TAIL.sub("", summary).strip(" ，,、；;|-_")
    if not summary:
        return ""
    ns, nt = normalize(summary), normalize(title)
    if not ns or not nt:
        return summary
    if ns == nt or nt.startswith(ns):
        return ""
    if ns.startswith(nt):
        # The title is a truncated prefix of the full text: keep the remainder.
        rest = _strip_title_prefix(summary, nt, title)
        rest = re.sub(r"^[\s\-—_|:：]*(?:市场参考|快讯|独家|原创|热点)?[\s\-—_|:：]*", "", rest)
        rest = rest.lstrip("】」）) :：，,、").strip(" ，,、；;:-_")
        return rest if len(rest) >= 12 else ""
    return summary

def normalize(title: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", title).lower()


# 跟踪参数会让同一篇文章出现多个 URL,破坏按 URL 去重。
TRACKING_PARAMS = {
    "oid", "vt", "cid", "node_id", "spm", "from", "source", "share_token",
    "at_medium", "at_campaign", "fbclid", "gclid", "ref", "cmpid", "oc",
    "wfr", "sr_share", "share_source", "s_channel", "spm_id_from",
}


def canonical_url(link: str) -> str:
    """去掉跟踪参数与锚点,让同一篇文章只对应一个 URL。"""
    if not link:
        return link
    parts = urllib.parse.urlsplit(link)
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=False)
        if key.lower() not in TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    path = parts.path
    if path.endswith("/") and path != "/":
        path = path.rstrip("/")
    return urllib.parse.urlunsplit(
        (
            parts.scheme or "https",
            parts.netloc.lower(),
            path,
            urllib.parse.urlencode(query),
            "",
        )
    )


def _numbers(text: str) -> set:
    """标题里的关键数字(含小数),忽略年份和个位月份一类弱特征。"""
    found = re.findall(r"\d+(?:\.\d+)?", text)
    return {n for n in found if len(n.replace(".", "")) >= 2 and n not in {"2024", "2025", "2026", "2027"}}


# 同一份统计数据的稿子常被改写标题,用「指标 + 数值 + 经济体」三元组识别。
DATA_SUBJECTS = (
    "gdp", "cpi", "ppi", "pmi", "失业率", "非农", "社融", "m2", "lpr",
    "工业增加值", "社会消费品零售",
)
COUNTRIES = ("中国", "美国", "欧元区", "日本", "德国", "英国", "韩国", "印度", "越南", "巴西")
# 同一笔融资/订单在不同媒体上的标题差异较大,用「公司 + 数字」做事件指纹。
EVENT_ENTITIES = (
    "openai", "anthropic", "英伟达", "台积电", "特斯拉", "苹果", "华为",
    "三星", "亚马逊", "微软", "meta", "谷歌", "deepmind", "字节跳动",
)
# 同一政策议题会被各家反复改写标题(如「出口管制/反倾销」),用高区分度术语的两两组合做话题指纹。
TOPIC_TERMS = (
    "政策利率", "隔夜逆回购", "利率走廊", "lpr", "存款准备金率", "降准",
    "出口管制", "反倾销", "制裁清单", "停火协议", "和平协议", "运价指数",
    "央行",
)


def _months(text: str) -> set:
    return {m for m in re.findall(r"(\d{1,2})月", text)}


def same_data_release(a: str, b: str) -> bool:
    subjects = {s for s in DATA_SUBJECTS if s in a and s in b}
    if not subjects:
        return False
    if not _numbers(a) & _numbers(b):
        return False
    countries_a = {c for c in COUNTRIES if c in a}
    countries_b = {c for c in COUNTRIES if c in b}
    if countries_a and countries_b and not countries_a & countries_b:
        return False
    months_a, months_b = _months(a), _months(b)
    if months_a and months_b and not months_a & months_b:
        return False
    return True


def same_event(a: str, b: str) -> bool:
    """同一事件的不同转载:标题高度重合(仅站点后缀/个别措辞不同)。"""
    if not a or not b:
        return False
    if same_data_release(a, b):
        return True
    if len(a) >= 16 and len(b) >= 16 and (a[:20] in b or b[:20] in a):
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 12 and short in long_:
        return True
    grams_a = {a[i:i + 3] for i in range(len(a) - 2)}
    grams_b = {b[i:i + 3] for i in range(len(b) - 2)}
    if not grams_a or not grams_b:
        return False
    overlap = len(grams_a & grams_b) / len(grams_a | grams_b)
    if overlap >= 0.8:
        return True
    # 同一条数据稿常被不同媒体改写结尾:前缀一致且关键数字相同即视为同一事件。
    prefix = 0
    for left, right in zip(a, b):
        if left != right:
            break
        prefix += 1
    if prefix >= 8 and _numbers(a) & _numbers(b):
        return True
    # 同一家公司的同一笔交易常被改写成不同标题(「加速推进300亿美元融资」vs「加速融资…」),
    # 公司名 + 关键数字相同即按同一事件处理。
    if any(name in a and name in b for name in EVENT_ENTITIES) and _numbers(a) & _numbers(b):
        return True
    # 长短两版转载(一方补了背景/引述)用二元组包含率兜底。
    if min(len(a), len(b)) >= 14:
        bigrams_a = {a[i:i + 2] for i in range(len(a) - 1)}
        bigrams_b = {b[i:i + 2] for i in range(len(b) - 1)}
        containment = len(bigrams_a & bigrams_b) / min(len(bigrams_a), len(bigrams_b))
        if containment >= 0.6:
            return True
    return False


def clean_title(title: str) -> str:
    title = re.sub(r"\s*[_|]\s*(?:手机)?新浪网.*$", "", title)
    # 快讯常见「【标题】正文……」格式:只保留方括号内的标题
    bracket = re.match(r"^【([^】]{8,80})】", title)
    if bracket:
        title = bracket.group(1).strip()
    title = re.sub(r"^(?:解读|分析|观察|深度|焦点)[｜|:：\-]\s*", "", title)
    # 金十等快讯会在标题尾部挂栏目名,如「…皆有可能-市场参考」
    title = re.sub(r"\s*[-—_|·]\s*(?:市场参考|快讯|独家|原创|热点)\s*$", "", title)
    for sep in ("|", "丨", "｜", "_"):
        if sep in title:
            head = title.split(sep)[0].strip()
            if len(head) >= 8:
                title = head
    return title.strip()


def is_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def shorten(text: str, limit: int = 190) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"(?<=[。！？!?])\s*", text)
    out = ""
    for sentence in sentences:
        if not sentence:
            continue
        if out and len(out) + len(sentence) > limit:
            break
        out += sentence
        if len(out) >= limit:
            break
    out = out.strip()
    if out and out[-1] not in "。！？!?…":
        cut = max((out.rfind(ch) for ch in "。！？!?"), default=-1)
        if cut >= 40:
            out = out[: cut + 1]
    if len(out) > limit:
        out = out[: limit - 1].rstrip("，,、；; ") + "…"
    return out


ASCII_TOKEN = re.compile(r"[A-Za-z0-9]{1,6}")


def kw_hit(keyword: str, text: str) -> bool:
    """ASCII 短词按词边界匹配,避免 "AI" 命中 "Gains"、"CPI" 命中 "SCPIxx"。"""
    if keyword == "AI":
        # OpenAI / Anthropic 一类复合词里的 AI 也要算命中
        return re.search(r"\bAI\b|OpenAI|Anthropic", text, re.I) is not None
    if ASCII_TOKEN.fullmatch(keyword):
        return re.search(rf"\b{re.escape(keyword)}\b", text, re.I) is not None
    return keyword in text


def classify(item: Dict[str, Any], hint: Optional[str]) -> Optional[str]:
    text = f"{item['title']} {item.get('summary', '')[:300]}"
    scores = {
        tag: sum(1 for kw in words if kw_hit(kw, text))
        for tag, words in CATEGORY_KEYWORDS.items()
    }
    best = max(scores, key=lambda tag: scores[tag])
    title = item["title"]
    title_hits = {
        tag: sum(1 for kw in words if kw_hit(kw, title))
        for tag, words in CATEGORY_KEYWORDS.items()
    }
    # 标题才是这条新闻的主语;只靠正文里顺带提到的关键词会被发布会通稿带偏。
    if title_hits[best] >= 1:
        return best
    titled = [tag for tag, hits in title_hits.items() if hits >= 1]
    if titled:
        return max(titled, key=lambda tag: (title_hits[tag], scores[tag]))
    # 标题完全没有类别关键词时,多半是通稿/发布会综述,正文偶尔提到一次不足以归入某一类。
    return None


STATEMENT = re.compile(r"(表示|认为|呼吁|建议|指出|称，)")
ACTION = re.compile(r"(宣布|签署|批准|发布|启动|加征|下调|上调|实施|通过|达成|生效|暂停|禁止|辞职|当选)")

# 「财联社7月9日讯」这类电头是可靠的发稿日期;部分聚合源不给 published,
# 不校验就会把几个月前的旧稿当成新热点。
DATELINE = re.compile(
    r"(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(?:讯|电(?![话视])|消息)"
)


def stale_dateline(
    item: Dict[str, Any], cutoff: datetime, now: datetime
) -> Optional[datetime]:
    """正文电头早于 cutoff 时返回该日期,用于丢弃旧稿。"""
    text = f"{item.get('title', '')} {item.get('summary', '')}"[:600]
    for match in DATELINE.finditer(text):
        year = int(match.group(1)) if match.group(1) else now.year
        month, day = int(match.group(2)), int(match.group(3))
        try:
            stamp = datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            continue
        # 无年份时按最近一次出现推断(跨年稿件)
        if not match.group(1) and stamp - now > timedelta(days=1):
            try:
                stamp = stamp.replace(year=year - 1)
            except ValueError:
                continue
        if stamp < cutoff - timedelta(days=1):
            return stamp
    return None


# 现场特稿/走访稿讲的是场景,不是刚发生的事实。
FEATURE = re.compile(r"(背后[:：]|见闻|探访|走访|蹲点|记者手记)")

# 「欧洲央行管委Rehn：法德利差有所扩大」这类官员表态是评论,不是事件。
OFFICIAL_QUOTE = re.compile(
    r"(管委|行长|主席|理事|发言人|官员|总裁|分析师|策略师|经济学家)[^：:]{0,8}[：:]"
)


def score_item(item: Dict[str, Any], now: datetime) -> float:
    text = f"{item['title']} {item.get('summary', '')[:200]}"
    value = 0.0
    for keyword, weight in POSITIVE.items():
        if keyword.lower() in text.lower():
            value += weight
    for keyword, weight in NEGATIVE.items():
        if keyword.lower() in text.lower():
            value += weight
    if NUMBER_RE.search(text):
        value += 2
    # 只有表态、没有任何落地动作的稿子(论坛发言、官员评论)降权
    if STATEMENT.search(text) and not ACTION.search(text):
        value -= 4
    # 疑问式标题多为前瞻/评论,不是已发生的事实
    if item["title"].rstrip().endswith(("？", "?")):
        value -= 5
    elif "？" in item["title"] or "?" in item["title"]:
        # 设问式标题(问号埋在标题中间)多为评论/预测,不是已发生的事实
        value -= 4
    if FEATURE.search(item["title"]):
        value -= 4
    if (
        OFFICIAL_QUOTE.search(item["title"])
        and not NUMBER_RE.search(item["title"])
        and not ACTION.search(item["title"])
    ):
        value -= 5
    summary = item.get("summary", "")
    if len(summary) >= 60 and not normalize(summary).startswith(normalize(item["title"])[:20]):
        value += 2
    elif len(summary) < 20:
        value -= 3
    if item.get("published"):
        age = (now - item["published"]).total_seconds() / 60
        value += 3 if age <= 90 else (1 if age <= 240 else -3)
    if not is_chinese(item["title"]):
        value -= 3
    if len(item["title"]) < 10:
        value -= 3
    weight = source_weight(item.get("link", ""), item.get("media", ""))
    item["source_weight"] = weight
    value += weight
    return value


HARD_SKIP = (
    "彩票", "球赛", "寒潮", "天气", "明星", "娱乐", "美食", "旅游",
    "早间要闻", "早知道", "点1氪", "早报", "午报", "晚报", "日报", "周报",
    # 券商评级/目标价是交易提示,不是新闻事件
    "评级上调", "评级下调", "目标价",
)

# 快讯站的「今日要闻」「盘前必读」是多条消息的拼盘,不是单一事件,不能推送。
ROUNDUP = re.compile(
    r"(?:今日|昨日|隔夜|盘前|盘中|早间|午间|晚间|周末|本周).{0,12}"
    r"(?:要闻|速递|汇总|盘点|必读|大事|前瞻)"
    r"|(?:要闻|速递|汇总|盘点|必读)\s*[】\]]"
    # 投资站的「市场动向解析」是盘前预告,不是已发生的事实
    r"|市场动向"
    r"|^\s*\d+\s*[、．]\s*"
    r"|\d+\s*[、．].{4,60}\d+\s*[、．]",
    re.I,
)


def is_roundup(title: str) -> bool:
    return bool(ROUNDUP.search(title))


def event_keys(item: Dict[str, Any]) -> set:
    """「公司 + 金额/数量」指纹,用于跨媒体识别同一事件。"""
    text = f"{item.get('title', '')} {item.get('summary', '')[:300]}".lower()
    entities = [name for name in EVENT_ENTITIES if name.lower() in text]
    if not entities:
        return set()
    numbers = set()
    for match in re.finditer(r"\d+(?:\.\d+)?", text):
        if match.group(0) in {"2024", "2025", "2026", "2027"}:
            continue
        if re.match(r"[月日年时分秒%]", text[match.end():match.end() + 1] or ""):
            continue
        value = match.group(0).split(".")[0]
        if len(value) >= 2:
            numbers.add(value)
    return {f"{name}:{num}" for name in entities for num in numbers}


def topic_keys(item: Dict[str, Any]) -> set:
    """同一政策议题的改写稿:命中的高区分度术语两两成对作为指纹。"""
    text = f"{item.get('title', '')} {item.get('summary', '')[:300]}".lower()
    hits = sorted({term for term in TOPIC_TERMS if term in text})
    if len(hits) < 2:
        return set()
    return {
        f"topic:{left}+{right}"
        for index, left in enumerate(hits)
        for right in hits[index + 1:]
    }


def fingerprints(item: Dict[str, Any]) -> set:
    return event_keys(item) | topic_keys(item)


def seen_fingerprints(state: Optional[Dict[str, Any]]) -> set:
    """历史指纹:已记录的事件 + 从历史标题反推的话题指纹(兼容旧状态文件)。"""
    state = state or {}
    keys = set(state.get("events", []))
    for title in state.get("titles", []):
        keys |= topic_keys({"title": title})
    return keys


def candidates(
    items: List[Dict[str, Any]],
    state: Dict[str, Any],
    now: datetime,
    max_age: int,
) -> Dict[str, List[Dict[str, Any]]]:
    seen_urls = {canonical_url(u) for u in state.get("urls", [])}
    seen_titles = set(state.get("titles", []))
    seen_norms = [normalize(t) for t in state.get("titles", []) if t]
    seen_events = seen_fingerprints(state)
    cutoff = now - timedelta(minutes=max_age)
    buckets: Dict[str, List[Dict[str, Any]]] = {tag: [] for tag, _ in CATEGORIES}

    for item in items:
        item["title"] = clean_title(item["title"])
        item["link"] = canonical_url(item["link"])
        title = item["title"]
        if len(title) < 8 or any(word in title for word in HARD_SKIP):
            continue
        if is_roundup(title):
            log(f"skip roundup [{title[:30]}]")
            continue
        if item.get("published") and item["published"] < cutoff:
            continue
        old = stale_dateline(item, cutoff, now)
        if old:
            log(f"skip stale dateline ({old:%Y-%m-%d}) [{title[:30]}]")
            continue
        if item["link"] in seen_urls or normalize(title) in seen_titles:
            continue
        norm_title = normalize(title)
        if any(same_event(norm_title, prev) for prev in seen_norms):
            continue
        if seen_events and fingerprints(item) & seen_events:
            log(f"skip seen event [{title[:30]}]")
            continue
        tag = classify(item, item.get("hint"))
        if not tag:
            continue
        item["category"] = tag
        score = score_item(item, now)
        if score < MIN_SCORE:
            log(f"skip low value ({score:+.0f}) [{tag}] {title[:40]}")
            continue
        item["score"] = score
        buckets[tag].append(item)

    for tag in buckets:
        buckets[tag].sort(key=lambda it: it["score"], reverse=True)
    return buckets


def pick(
    buckets: Dict[str, List[Dict[str, Any]]],
    max_items: int,
    enrich_budget: int = 14,
    state: Optional[Dict[str, Any]] = None,
    max_age: int = 240,
) -> List[Dict[str, Any]]:
    """Enrich the best candidate per category, one category at most once."""
    picked: List[Dict[str, Any]] = []
    used_titles = set()
    used_links = set()
    budget = enrich_budget
    # 事件指纹在富化后才完整(金额/数量常来自正文),所以这里再核对一次历史记录。
    seen_events = seen_fingerprints(state)
    seen_norms = [normalize(t) for t in (state or {}).get("titles", []) if t]

    for tag, _ in CATEGORIES:
        if len(picked) >= max_items or budget <= 0:
            break
        for candidate in buckets.get(tag, [])[:5]:
            key = normalize(candidate["title"])
            if key in used_titles or candidate["link"] in used_links:
                continue
            budget -= 1
            enriched = enrich(dict(candidate))
            if not enriched:
                continue
            enriched["title"] = enriched["title"].replace("\n", " ").strip()
            old = stale_dateline(
                enriched,
                datetime.now(timezone.utc) - timedelta(minutes=max_age),
                datetime.now(timezone.utc),
            )
            if old:
                log(f"skip stale dateline after enrich ({old:%Y-%m-%d}) [{enriched['title'][:30]}]")
                continue
            norm = normalize(enriched["title"])
            keys = fingerprints(enriched)
            if keys & seen_events:
                log(f"skip seen event after enrich [{enriched['title'][:30]}]")
                continue
            if any(same_event(norm, prev) for prev in seen_norms):
                log(f"skip seen topic after enrich [{enriched['title'][:30]}]")
                continue
            # Google News 条目在打分时还是跳转域名,解析出真实站点后要再核一次信源质量。
            if source_weight(enriched.get("link", "")) <= -5:
                log(f"skip low-trust source after resolve [{enriched['title'][:30]}]")
                continue
            used_titles.add(key)
            used_links.add(enriched["link"])
            seen_events |= keys
            picked.append(enriched)
            break
    return picked


def build_blocks(items: List[Dict[str, Any]]) -> List[str]:
    blocks: List[str] = []
    for index, item in enumerate(items, start=1):
        title = html.escape(shorten(item["title"], 90))
        summary = shorten(item.get("summary", ""), 190)
        if normalize(summary).startswith(normalize(item["title"])[:20]):
            summary = summary[len(item["title"]):].lstrip("：:，,。；;、 ")
        summary = shorten(summary, 170)
        if summary and summary[-1] not in "。！？!?….…":
            summary += "…"
        summary = html.escape(summary)
        media = html.escape(item.get("media") or "原文")
        link = html.escape(item["link"], quote=True)
        block = [f"<b>{index}. [{item['category']}] {title}</b>"]
        if summary:
            block.append(summary)
        block.append(f'<a href="{link}">{media}</a>')
        blocks.append("\n".join(block))
    return blocks


def build_messages(
    items: List[Dict[str, Any]], now: Optional[datetime] = None
) -> List[str]:
    stamp = (now or datetime.now(timezone.utc)).astimezone(BEIJING).strftime(
        "%Y-%m-%d %H:%M"
    )
    header = f"<b>🔥 要闻速递 · AI / 外贸 / 经济 / 国际</b>\n<i>北京时间 {stamp}</i>"
    messages: List[str] = []
    current = header
    for block in build_blocks(items):
        if len(current) + len(block) + 2 > MAX_MESSAGE_CHARS:
            messages.append(current)
            current = f"<b>🔥 要闻速递（续）</b>\n<i>北京时间 {stamp}</i>\n\n{block}"
        else:
            current = f"{current}\n\n{block}"
    messages.append(current)
    return messages


def send_via_script(token: str, chat_id: str, text: str) -> None:
    path = "/tmp/digest.html"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    if not os.path.isfile(TELEGRAM_SCRIPT):
        raise NewsError(f"missing sender script: {TELEGRAM_SCRIPT}")
    env = dict(os.environ)
    env["TELEGRAM_BOT_TOKEN"] = token
    result = subprocess.run(
        [
            sys.executable,
            TELEGRAM_SCRIPT,
            "send",
            "--chat-id",
            str(chat_id),
            "--parse-mode",
            "HTML",
            "--text-file",
            path,
            "--no-preview",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    if result.returncode != 0:
        raise NewsError(f"telegram.py failed: {result.stderr.strip()[:300]}")


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


def collect_all(max_age: int) -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    jobs: List[Tuple[str, str, str]] = []
    for tag, queries in CATEGORIES:
        for query in queries:
            jobs.append(("google", query, tag))
    for tag, url in DIRECT_FEEDS:
        jobs.append(("feed", url, tag))
    for name, url in LIVE_ENDPOINTS:
        jobs.append(("live", f"{name}|{url}", ""))

    items: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures: List[Tuple[Any, Optional[str]]] = []
        for kind, value, tag in jobs:
            hint = tag or None
            if kind == "google":
                futures.append((pool.submit(collect_google_news, value), hint))
            elif kind == "feed":
                host = urllib.parse.urlparse(value).netloc
                futures.append((pool.submit(collect_feed, host, value), hint))
            else:
                name, url = value.split("|", 1)
                futures.append((pool.submit(collect_live, name, url), hint))
        for future, hint in futures:
            try:
                for item in future.result():
                    item["hint"] = hint
                    items.append(item)
            except Exception as exc:  # noqa: BLE001 - keep the run alive
                log(f"collector crashed: {exc}")

    # De-duplicate identical stories coming from several sources.
    best: Dict[str, Dict[str, Any]] = {}
    for item in items:
        key = normalize(item["title"])[:40]
        if not key:
            continue
        previous = best.get(key)
        if previous is None or len(item.get("summary", "")) > len(previous.get("summary", "")):
            best[key] = item
    log(f"collected {len(items)} raw items, {len(best)} unique")
    return list(best.values())



def _record_push(state: Dict[str, Any], items: List[Dict[str, Any]]) -> None:
    state["urls"].extend(item["link"] for item in items)
    state["titles"].extend(normalize(item["title"]) for item in items)
    state.setdefault("events", []).extend(
        key for item in items for key in sorted(fingerprints(item))
    )
    state["events"] = state["events"][-200:]
    state["empty_runs"] = 0
    state["last_push"] = datetime.now(timezone.utc).isoformat()
    state.pop("last_error", None)


def run(args: argparse.Namespace) -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not args.dry_run and not token:
        raise NewsError("TELEGRAM_BOT_TOKEN is not set")

    state = load_state(args.state)
    now = datetime.now(timezone.utc)

    items = collect_all(args.max_age)
    if STATS["ok"] == 0:
        reason = f"{STATS['fail']} 个新闻源请求全部失败"
        log(f"error: {reason}")
        if args.dry_run:
            return 1
        if state.get("last_error") != reason:
            text = (
                "<b>⚠️ 要闻速递抓取失败</b>\n"
                f"{reason}，本次未推送。下轮自动重试。"
            )
            try:
                send_via_script(token, args.chat_id, text)
                state["last_error"] = reason
                state["last_push"] = datetime.now(timezone.utc).isoformat()
                save_state(args.state, state)
            except NewsError as exc:
                log(f"error report failed: {exc}")
        return 1

    buckets = candidates(items, state, now, args.max_age)
    picked = pick(buckets, args.max_items, state=state, max_age=args.max_age)
    coverage = {item["category"] for item in picked}

    if len(picked) < 2 or len(coverage) < 2:
        state["empty_runs"] = int(state.get("empty_runs", 0)) + 1
        log(
            f"not enough fresh material (items={len(picked)}, "
            f"categories={sorted(coverage)}, empty_runs={state['empty_runs']})"
        )
        if state["empty_runs"] >= args.heartbeat_after:
            log("heartbeat due")
            if not args.dry_run:
                send_via_script(
                    token,
                    args.chat_id,
                    "<b>💤 心跳</b>\n过去 1 小时四类均无新增热点，任务运行正常。",
                )
                state["empty_runs"] = 0
                state["last_push"] = datetime.now(timezone.utc).isoformat()
                save_state(args.state, state)
            return 0
        if not args.dry_run:
            save_state(args.state, state)
        return 0

    picked.sort(key=lambda it: [tag for tag, _ in CATEGORIES].index(it["category"]))
    messages = build_messages(picked, now)
    if args.dry_run:
        print("\n\n---\n\n".join(messages))
        print(
            f"\n# dry-run: {len(picked)} item(s), {len(messages)} message(s)",
            file=sys.stderr,
        )
        return 0

    for message in messages:
        send_via_script(token, args.chat_id, message)
    log(f"sent {len(messages)} message(s) with {len(picked)} item(s)")

    _record_push(state, picked)
    save_state(args.state, state)
    for item in picked:
        log(f"pushed [{item['category']}] {item['title'][:60]} -> {item['link']}")
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
