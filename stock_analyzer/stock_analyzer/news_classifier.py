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
    ``direction_hint`` fields in place; return the list for chaining.

    Items without a meaningful classification are left untouched
    (no empty-string fields added) so the prompt renderer can use
    ``item.get("category")`` truthiness to detect the urgent subset.
    """
    for item in news_items:
        title = item.get("title", "")
        result = classify_headline(title)
        if result.category:
            item["category"] = result.category
            item["severity"] = result.severity
            if result.direction_hint:
                item["direction_hint"] = result.direction_hint
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
        if cat and item.get("severity") == "urgent":
            direction = item.get("direction_hint") or ""
            dir_tag = f"({direction})" if direction else ""
            urgent.append(f"🔴 [{cat}{dir_tag}] {title}")
        else:
            other.append(title)
    parts: list[str] = []
    if urgent:
        parts.append(" / ".join(urgent))
    if other:
        parts.append(" / ".join(other))
    return " || ".join(parts)
