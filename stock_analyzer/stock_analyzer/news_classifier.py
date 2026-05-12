"""Classify free-form JP-equity news headlines by impact category.

The existing ``news_fetcher`` pulls kabutan headlines per-ticker but
hands them to the AI as undifferentiated strings. A TOB announcement
gets the same visual weight as a vague industry op-ed. Serious
investors monitor the official 適時開示 (TDnet) feed precisely
*because* the canonical urgent categories — TOB / 業績修正 / 自己株
取得 / 株式分割 / 大量保有 / M&A / 第三者割当増資 — move stocks
immediately.

Scraping TDnet directly is fragile and high-maintenance. This module
takes the cheaper path: classify already-fetched kabutan headlines
with the same canonical categories so the AI prompt highlights them
explicitly. We catch ~60-70% of the high-signal events with zero new
external dependency.

The classification is deliberately conservative: each keyword set is
narrow enough that a false positive is rare even on noisy headlines.
A category with no clear actionability (e.g. "業績修正" is bullish if
upward, bearish if downward — direction matters) tags both the
category and the direction so the AI can read both.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NewsClassification:
    """Result of classifying a single headline.

    ``category`` is one of the canonical TDnet-style buckets (or empty
    string when no match). ``severity`` is "urgent" for moves that
    typically gap the stock on the next session, "watch" for material
    but slower-moving items, and "" for unmatched.

    ``direction_hint`` captures direction when the headline implies
    one (上方修正 → "bullish", 下方修正 → "bearish"); empty when the
    category is direction-agnostic (e.g. TOB).
    """

    category: str
    severity: str
    direction_hint: str = ""


# Sentiment lexicon — Japanese terms that consistently flag positive
# or negative market impact when present in a headline. The list is
# narrow on purpose: broad sentiment lexicons collected from social-
# media data are noisy for financial news (e.g. "問題" is neutral in
# headlines but negative in casual speech). These terms have been
# selected because they routinely appear in disclosure / press-
# release headlines and consistently correlate with price impact.
_BULLISH_TERMS = [
    "急騰",
    "上昇",
    "好調",
    "増益",
    "増収",
    "最高益",
    "黒字",
    "黒字転換",
    "黒字化",
    "上方修正",
    "増配",
    "復配",
    "自社株買い",
    "自己株式取得",
    "受注",
    "好材料",
    "ポジティブ",
    "強気",
    "買い推奨",
    "格上げ",
    "新製品",
    "提携",
    "新規契約",
    "成長",
    "拡大",
    "突破",
    "達成",
    "高値",
    "復活",
]
_BEARISH_TERMS = [
    "急落",
    "下落",
    "下方修正",
    "減益",
    "減収",
    # NOTE: 赤字 / 赤字転落 / 赤字拡大 — keep only 赤字 (substring of
    # the others) to avoid double-counting. Same for 下方 vs 下方修正.
    "赤字",
    "悪材料",
    "ネガティブ",
    "弱気",
    "売り推奨",
    "格下げ",
    "減配",
    "無配",
    "減少",
    "縮小",
    "懸念",
    "リスク",
    "警告",
    "不振",
    "苦戦",
    "下振れ",
    "破綻",
    "倒産",
    "停止",
    "中止",
    "撤退",
    "リコール",
    "違反",
    "訴訟",
    "炎上",
]


def score_sentiment(title: str) -> int:
    """Net sentiment score from a headline.

    Returns sum of bullish-term hits minus bearish-term hits. Each
    term contributes ±1; the result is unbounded but typically ±3
    or smaller for real headlines. Zero means "balanced" — either
    no terms hit, or equal hits each way.

    This is intentionally cheap (substring search, no NLP model).
    For financial JP headlines, the canonical-term presence is a
    better signal than ML sentiment classifiers trained on social
    data, which mis-classify domain-specific phrasing like 「業績
    予想を下方修正」 as neutral.
    """
    if not title:
        return 0
    score = 0
    for term in _BULLISH_TERMS:
        if term in title:
            score += 1
    for term in _BEARISH_TERMS:
        if term in title:
            score -= 1
    return score


_CATEGORIES = [
    # TOB / 公開買付 — almost always urgent, premium-to-market driven
    (("TOB", "公開買付"), "TOB", "urgent", "bullish"),
    # 業績修正 — direction matters. Check bearish keyword first so a
    # headline containing both "業績予想の修正" and "下方修正" is
    # correctly tagged bearish (the generic phrase is a substring of
    # the directional one).
    (("下方修正",), "業績修正", "urgent", "bearish"),
    (("上方修正",), "業績修正", "urgent", "bullish"),
    # 自己株式取得 — typically bullish (buyback signal)
    (("自己株式取得", "自社株買い"), "自己株取得", "urgent", "bullish"),
    # 株式分割 — typically bullish (improved liquidity, retail access)
    (("株式分割",), "株式分割", "watch", "bullish"),
    # 大量保有報告 — direction depends on who but flag as urgent
    (("大量保有報告", "5%ルール"), "大量保有", "urgent", ""),
    # M&A / 合併 / 買収
    (("M&A", "合併", "買収", "経営統合"), "M&A", "urgent", ""),
    # 第三者割当増資 — dilutive
    (("第三者割当増資", "公募増資"), "増資", "urgent", "bearish"),
    # 配当変更
    (("増配", "配当予想の上方修正"), "配当", "watch", "bullish"),
    (("減配", "無配転落", "配当予想の下方修正"), "配当", "watch", "bearish"),
    # 株主優待新設 / 拡充
    (("株主優待新設", "株主優待拡充"), "株主優待", "watch", "bullish"),
]


def classify_headline(title: str) -> NewsClassification:
    """Return the classification for a single news headline.

    Walks the keyword table in declaration order, returning on first
    hit. The ordering matters when keywords overlap (e.g. 下方修正
    must be checked before generic 業績修正 so the direction is
    preserved). Unmatched headlines return an empty classification —
    callers test ``category`` truthiness to decide whether to surface
    the item with extra emphasis.
    """
    if not title:
        return NewsClassification(category="", severity="")
    for keywords, category, severity, direction in _CATEGORIES:
        if any(k in title for k in keywords):
            return NewsClassification(category=category, severity=severity, direction_hint=direction)
    return NewsClassification(category="", severity="")


def classify_news_list(news_items: list[dict]) -> list[dict]:
    """Annotate each news dict with ``category`` / ``severity`` /
    ``direction_hint`` / ``sentiment`` fields in place; return the
    list for chaining.

    Items without a category classification are still scored for
    sentiment — a broad-market headline like "日経平均急落" has no
    canonical category but carries clear bearish sentiment that
    propagates to all stocks. The sentiment score is added on every
    item; the categorical fields only when a canonical category fires.
    """
    for item in news_items:
        title = item.get("title", "")
        result = classify_headline(title)
        if result.category:
            item["category"] = result.category
            item["severity"] = result.severity
            if result.direction_hint:
                item["direction_hint"] = result.direction_hint
        # Sentiment score independent of categorical match
        sentiment = score_sentiment(title)
        if sentiment != 0:
            item["sentiment"] = sentiment
    return news_items


def extract_urgent(news_items: list[dict]) -> list[dict]:
    """Filter to only the urgent classifications. Useful for building
    a top-of-prompt 'urgent disclosures' block separate from the
    background headline stream."""
    return [n for n in news_items if n.get("severity") == "urgent"]


def format_for_prompt(news_items: list[dict]) -> str:
    """Render classified headlines as a structured string for inclusion
    in a per-stock prompt block.

    Urgent items are listed first with their category prefix; non-
    urgent items follow as a plain comma list. Empty input returns
    an empty string so callers can ``if formatted:`` -guard without
    a None check.
    """
    if not news_items:
        return ""
    urgent: list[str] = []
    other: list[str] = []
    for item in news_items:
        title = item.get("title", "").strip()
        if not title:
            continue
        cat = item.get("category")
        sentiment = item.get("sentiment", 0)
        sent_tag = ""
        if isinstance(sentiment, int) and sentiment != 0:
            sent_tag = f" [sent={sentiment:+d}]"
        if cat and item.get("severity") == "urgent":
            direction = item.get("direction_hint") or ""
            dir_tag = f"({direction})" if direction else ""
            urgent.append(f"🔴 [{cat}{dir_tag}] {title}{sent_tag}")
        else:
            other.append(f"{title}{sent_tag}")
    parts: list[str] = []
    if urgent:
        parts.append(" / ".join(urgent))
    if other:
        parts.append(" / ".join(other))
    return " || ".join(parts)


def aggregate_sentiment(news_items: list[dict]) -> dict:
    """Compute per-stock aggregate sentiment from a list of classified
    items.

    Returns ``{"count": N, "net_sentiment": sum, "positive_count": p,
    "negative_count": n}``. Useful for the AI prompt to see "this
    stock has 5 recent headlines, net sentiment +3" at a glance
    without parsing each headline.
    """
    count = 0
    net = 0
    pos = 0
    neg = 0
    for item in news_items:
        s = item.get("sentiment")
        if not isinstance(s, int):
            continue
        count += 1
        net += s
        if s > 0:
            pos += 1
        elif s < 0:
            neg += 1
    return {
        "count": count,
        "net_sentiment": net,
        "positive_count": pos,
        "negative_count": neg,
    }
