from __future__ import annotations

import logging
import re

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
_TIMEOUT = 15

# AI-related keywords for filtering. Short tokens ("ai", "ml", "gpt",
# "llm", "rag", "nlp", "mcp") must word-boundary match — substring
# match on "ai" silently scoops "Gmail" / "fail" / "nail" / "Maryland"
# and the buzz layer fills with noise. Longer keywords are matched as
# substrings (case-insensitive) which is safe enough.
_AI_KEYWORDS_WORDBOUND = [
    "ai",
    "ml",
    "gpt",
    "llm",
    "rag",
    "nlp",
    "mcp",
    "kv",
]
_AI_KEYWORDS_SUBSTRING = [
    "artificial intelligence",
    "machine learning",
    "deep learning",
    "large language model",
    "claude",
    "gemini",
    "openai",
    "anthropic",
    "transformer",
    "neural",
    "computer vision",
    "diffusion",
    "generative",
    "agent",
    "fine-tune",
    "embedding",
    "chatbot",
    "copilot",
    "model",
    "inference",
    "pytorch",
    "tensorflow",
    "hugging face",
    "langchain",
    "vector database",
    "prompt",
    "reasoning",
    "multimodal",
    "foundation model",
    "reinforcement learning",
    "tool use",
    "codex",
    "agentic",
]

_WORDBOUND_REGEX = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _AI_KEYWORDS_WORDBOUND) + r")\b",
    re.IGNORECASE,
)


def _is_ai_related(text: str) -> bool:
    """Combined keyword check — word-boundary for short tokens, substring
    for the rest. The split prevents 'ai' matching 'Gmail' / 'nail' /
    'Maryland' which was poisoning the HN buzz layer."""
    if not text:
        return False
    lower = text.lower()
    if any(k in lower for k in _AI_KEYWORDS_SUBSTRING):
        return True
    return bool(_WORDBOUND_REGEX.search(text))


# Backwards-compat alias used by existing call sites. New code should
# call ``_is_ai_related`` directly.
_AI_KEYWORDS = _AI_KEYWORDS_SUBSTRING + _AI_KEYWORDS_WORDBOUND


def fetch_hackernews_ai(max_items: int = 15) -> list[dict]:
    """Fetch AI-related top stories from Hacker News."""
    try:
        resp = requests.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json",
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("HN topstories returned %d", resp.status_code)
            return []

        story_ids = resp.json()[:100]  # Check top 100 stories
        ai_stories: list[dict] = []

        for story_id in story_ids:
            if len(ai_stories) >= max_items:
                break
            try:
                item_resp = requests.get(
                    f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json",
                    timeout=_TIMEOUT,
                )
                if item_resp.status_code != 200:
                    continue
                item = item_resp.json()
                if not item or item.get("type") != "story":
                    continue

                title = item.get("title", "")
                if _is_ai_related(title):
                    ai_stories.append(
                        {
                            "title": item.get("title", ""),
                            "url": item.get("url", f"https://news.ycombinator.com/item?id={story_id}"),
                            "score": item.get("score", 0),
                            "comments": item.get("descendants", 0),
                            "source": "Hacker News",
                        }
                    )
            except Exception:
                continue

        logger.info("Fetched %d AI stories from HN", len(ai_stories))
        return ai_stories
    except Exception as e:
        logger.warning("Failed to fetch HN: %s", e)
        return []


def fetch_github_trending_ai(max_items: int = 10) -> list[dict]:
    """Fetch AI-related trending repos from GitHub."""
    try:
        resp = requests.get(
            "https://github.com/trending?since=daily",
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("GitHub trending returned %d", resp.status_code)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        repos: list[dict] = []

        for article in soup.select("article.Box-row"):
            if len(repos) >= max_items:
                break

            # Repo name
            h2 = article.select_one("h2 a")
            if not h2:
                continue
            repo_name = h2.get_text(strip=True).replace("\n", "").replace(" ", "")

            # Description
            p = article.select_one("p")
            desc = p.get_text(strip=True) if p else ""

            # Check if AI-related (word-boundary for short tokens)
            combined = f"{repo_name} {desc}"
            if not _is_ai_related(combined):
                continue

            # Stars today
            stars_today = ""
            span = article.select_one("span.d-inline-block.float-sm-right")
            if span:
                stars_today = span.get_text(strip=True)

            # Language
            lang_span = article.select_one("[itemprop='programmingLanguage']")
            language = lang_span.get_text(strip=True) if lang_span else ""

            repos.append(
                {
                    "name": repo_name,
                    "description": desc[:200],
                    "language": language,
                    "stars_today": stars_today,
                    "url": f"https://github.com/{repo_name}",
                    "source": "GitHub Trending",
                }
            )

        logger.info("Fetched %d AI repos from GitHub trending", len(repos))
        return repos
    except Exception as e:
        logger.warning("Failed to fetch GitHub trending: %s", e)
        return []


def fetch_arxiv_ai(max_items: int = 15) -> list[dict]:
    """Fetch recent AI/ML papers from arXiv."""
    try:
        resp = requests.get(
            "http://export.arxiv.org/api/query",
            params={
                "search_query": "cat:cs.AI OR cat:cs.CL OR cat:cs.LG",
                "sortBy": "submittedDate",
                "sortOrder": "descending",
                "max_results": max_items,
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("arXiv API returned %d", resp.status_code)
            return []

        soup = BeautifulSoup(resp.text, "xml")
        papers: list[dict] = []

        for entry in soup.find_all("entry"):
            title = entry.find("title")
            summary = entry.find("summary")
            link = entry.find("id")
            authors = entry.find_all("author")

            author_names = [a.find("name").get_text(strip=True) for a in authors[:3]]
            if len(authors) > 3:
                author_names.append(f"他{len(authors) - 3}名")

            papers.append(
                {
                    "title": title.get_text(strip=True) if title else "",
                    "summary": summary.get_text(strip=True)[:300] if summary else "",
                    "authors": ", ".join(author_names),
                    "url": link.get_text(strip=True) if link else "",
                    "source": "arXiv",
                }
            )

        logger.info("Fetched %d papers from arXiv", len(papers))
        return papers
    except Exception as e:
        logger.warning("Failed to fetch arXiv: %s", e)
        return []


def fetch_ai_company_news(max_per_source: int = 5) -> list[dict]:
    """Fetch latest news from major AI companies: Anthropic, OpenAI, Google."""
    results: list[dict] = []

    # Anthropic (direct scrape)
    try:
        resp = requests.get(
            "https://www.anthropic.com/news",
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            seen: set[str] = set()
            for a in soup.select("a[href*='/news/']"):
                title = a.get_text(strip=True)
                href = a.get("href", "")
                if len(title) > 15 and title not in seen and len(seen) < max_per_source:
                    seen.add(title)
                    url = f"https://www.anthropic.com{href}" if href.startswith("/") else href
                    results.append(
                        {
                            "title": title,
                            "url": url,
                            "source": "Anthropic",
                        }
                    )
    except Exception as e:
        logger.debug("Failed to fetch Anthropic news: %s", e)

    # OpenAI (RSS feed)
    try:
        resp = requests.get(
            "https://openai.com/blog/rss.xml",
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "xml")
            for item in soup.find_all("item")[:max_per_source]:
                title = item.find("title")
                link = item.find("link")
                if title:
                    results.append(
                        {
                            "title": title.get_text(strip=True),
                            "url": link.get_text(strip=True) if link else "",
                            "source": "OpenAI",
                        }
                    )
    except Exception as e:
        logger.debug("Failed to fetch OpenAI news: %s", e)

    # Google AI Blog
    try:
        resp = requests.get(
            "https://blog.google/technology/ai/",
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            seen_g: set[str] = set()
            for a in soup.select("a[href*='/technology/ai/']"):
                title = a.get_text(strip=True)
                href = a.get("href", "")
                if (
                    len(title) > 20
                    and href != "/technology/ai/"
                    and title not in seen_g
                    and len(seen_g) < max_per_source
                ):
                    seen_g.add(title)
                    url = f"https://blog.google{href}" if href.startswith("/") else href
                    results.append(
                        {
                            "title": title,
                            "url": url,
                            "source": "Google AI",
                        }
                    )
    except Exception as e:
        logger.debug("Failed to fetch Google AI news: %s", e)

    # Meta AI Blog
    try:
        resp = requests.get(
            "https://ai.meta.com/blog/",
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            seen_m: set[str] = set()
            for a in soup.select("a[href*='/blog/']"):
                title = a.get_text(strip=True)
                href = a.get("href", "")
                if len(title) > 20 and href != "/blog/" and title not in seen_m and len(seen_m) < max_per_source:
                    seen_m.add(title)
                    url = f"https://ai.meta.com{href}" if href.startswith("/") else href
                    results.append(
                        {
                            "title": title,
                            "url": url,
                            "source": "Meta AI",
                        }
                    )
    except Exception as e:
        logger.debug("Failed to fetch Meta AI news: %s", e)

    # Microsoft AI Blog
    try:
        resp = requests.get(
            "https://blogs.microsoft.com/ai/",
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            seen_ms: set[str] = set()
            for a in soup.select("a[href*='blogs.microsoft.com']"):
                title = a.get_text(strip=True)
                if len(title) > 20 and title not in seen_ms and len(seen_ms) < max_per_source:
                    seen_ms.add(title)
                    results.append(
                        {
                            "title": title,
                            "url": a.get("href", ""),
                            "source": "Microsoft AI",
                        }
                    )
    except Exception as e:
        logger.debug("Failed to fetch Microsoft AI news: %s", e)

    # Vercel Blog
    try:
        resp = requests.get(
            "https://vercel.com/blog",
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            seen_v: set[str] = set()
            for a in soup.select("a[href*='/blog/']"):
                title = a.get_text(strip=True)
                href = a.get("href", "")
                if len(title) > 20 and href != "/blog/" and title not in seen_v and len(seen_v) < max_per_source:
                    combined = title.lower()
                    if any(kw in combined for kw in ["ai", "model", "sdk", "next", "v0", "agent"]):
                        seen_v.add(title)
                        url = f"https://vercel.com{href}" if href.startswith("/") else href
                        results.append(
                            {
                                "title": title,
                                "url": url,
                                "source": "Vercel",
                            }
                        )
    except Exception as e:
        logger.debug("Failed to fetch Vercel blog: %s", e)

    logger.info("Fetched %d AI company news items", len(results))
    return results


def fetch_ai_tools_releases(max_items: int = 10) -> list[dict]:
    """Fetch latest releases/changelogs from major AI developer tools.

    Repos flagged with priority=True are user must-watch — their releases
    bypass the analyzer's importance filter so version bumps + feature
    notes always reach the daily digest. Body truncation is generous
    enough (1500 chars) to keep the actual changelog intact.
    """
    results: list[dict] = []

    # GitHub releases for key AI tool repos. priority=True → always surface.
    tool_repos: list[tuple[str, str, bool]] = [
        # Anthropic — CLAUDE CODE IS MUST-WATCH
        ("anthropics/claude-code", "Claude Code", True),
        ("anthropics/anthropic-sdk-python", "Anthropic Python SDK", True),
        ("anthropics/anthropic-sdk-typescript", "Anthropic TS SDK", False),
        ("anthropics/courses", "Anthropic Courses", False),
        ("modelcontextprotocol/servers", "MCP Servers", False),
        ("modelcontextprotocol/python-sdk", "MCP Python SDK", False),
        # OpenAI — CODEX IS MUST-WATCH
        ("openai/codex", "OpenAI Codex", True),
        ("openai/openai-python", "OpenAI Python SDK", False),
        ("openai/openai-agents-python", "OpenAI Agents SDK", False),
        ("openai/whisper", "Whisper", False),
        # Google — GEMINI CLI IS MUST-WATCH
        ("google-gemini/gemini-cli", "Gemini CLI", True),
        ("google-gemini/cookbook", "Gemini Cookbook", False),
        ("google/generative-ai-python", "Google GenAI SDK", False),
        # Meta
        ("meta-llama/llama-models", "Llama Models", False),
        ("meta-llama/llama-stack", "Llama Stack", False),
        # Vercel / Next.js
        ("vercel/ai", "Vercel AI SDK", False),
        ("vercel/next.js", "Next.js", False),
        ("vercel/ai-chatbot", "Vercel AI Chatbot", False),
        # Ecosystem
        ("langchain-ai/langchain", "LangChain", False),
        ("run-llama/llama_index", "LlamaIndex", False),
        ("huggingface/transformers", "HuggingFace Transformers", False),
        ("vllm-project/vllm", "vLLM", False),
        ("ollama/ollama", "Ollama", False),
        ("microsoft/autogen", "AutoGen", False),
        ("crewAIInc/crewAI", "CrewAI", False),
    ]

    body_chars = 1500  # was 200 — too aggressive, important features were truncated

    for repo, name, priority in tool_repos:
        try:
            resp = requests.get(
                f"https://api.github.com/repos/{repo}/releases",
                headers={**_HEADERS, "Accept": "application/vnd.github.v3+json"},
                timeout=_TIMEOUT,
                params={"per_page": 2},
            )
            if resp.status_code != 200:
                continue
            releases = resp.json()
            for rel in releases[:1]:  # Latest release only
                tag = rel.get("tag_name", "")
                body = (rel.get("body") or "")[:body_chars]
                published = rel.get("published_at", "")[:10]
                results.append(
                    {
                        "title": f"{name} {tag}",
                        "version": tag,
                        "published": published,
                        "changelog": body,
                        "url": rel.get("html_url", ""),
                        "source": "GitHub Releases",
                        "priority": priority,
                    }
                )
        except Exception:
            continue

    logger.info("Fetched %d AI tool releases (%d priority)", len(results), sum(1 for r in results if r.get("priority")))
    return results


_FOCUSED_TOOLS: list[dict] = [
    {
        "name": "Claude Code",
        "repo": "anthropics/claude-code",
        "blog_url": "https://www.anthropic.com/news",
        "blog_filter_keywords": ["claude code", "claude-code"],
        "blog_source_label": "Anthropic",
    },
    {
        "name": "OpenAI Codex",
        "repo": "openai/codex",
        "blog_url": "https://openai.com/blog/rss.xml",
        "blog_filter_keywords": ["codex"],
        "blog_source_label": "OpenAI",
    },
    {
        "name": "Gemini CLI",
        "repo": "google-gemini/gemini-cli",
        "blog_url": "https://blog.google/technology/developers/",
        "blog_filter_keywords": ["gemini cli", "gemini-cli", "gemini 2", "gemini api"],
        "blog_source_label": "Google Developers",
    },
]


def fetch_focused_tool_updates(lookback_hours: int = 6) -> list[dict]:
    """Fetch recent updates for Claude Code / Codex / Gemini CLI only.

    Three classes of update per tool, all filtered to the last
    ``lookback_hours``:

    1. **GitHub releases** — official version cuts with full changelog
    2. **Recent commits to default branch** — pre-release activity that
       sometimes signals "feature is shipping today" before the tag drops
    3. **Vendor blog posts** — Anthropic / OpenAI / Google Developers
       filtered for the tool's keyword set

    The caller (``main.phase_gather``) checks the returned list length:
    empty → no new releases / commits / blog posts in window → exit
    silently without spamming Slack. This is the "timely without
    noisy" trade-off — the user trusts an absent post to mean
    "nothing new" rather than reading the same digest twice.

    Each item dict carries: ``tool`` / ``kind`` (release|commit|blog)
    / ``title`` / ``summary`` / ``url`` / ``published_iso`` / ``source``.
    """
    from datetime import UTC, datetime, timedelta

    cutoff = datetime.now(UTC) - timedelta(hours=lookback_hours)
    results: list[dict] = []

    for tool in _FOCUSED_TOOLS:
        results.extend(_fetch_github_releases_recent(tool, cutoff))
        results.extend(_fetch_github_commits_recent(tool, cutoff))
        results.extend(_fetch_vendor_blog_recent(tool, cutoff))

    # Stable sort: newest first so the prompt leads with the most-recent
    # update. Items without parseable timestamps fall to the back.
    results.sort(key=lambda r: r.get("published_iso", ""), reverse=True)
    logger.info(
        "fetch_focused_tool_updates: %d items in last %dh (%s)",
        len(results),
        lookback_hours,
        ", ".join(f"{r['tool']}/{r['kind']}" for r in results[:10]),
    )
    return results


def _fetch_github_releases_recent(tool: dict, cutoff_dt: object) -> list[dict]:
    """GitHub releases for one tool, filtered to those after ``cutoff_dt``.

    Pre-release dedup: when multiple alpha / RC / dev tags share the
    same base version (``X.Y.Z``) inside the lookback window, keep
    only the most-recent one. Otherwise a release cadence like
    0.131.0-alpha.2 / .4 / .6 within a couple of hours floods the
    digest with what is functionally one release in progress. Stable
    tags are always kept regardless.
    """
    from datetime import datetime

    out: list[dict] = []
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{tool['repo']}/releases",
            headers={**_HEADERS, "Accept": "application/vnd.github.v3+json"},
            timeout=_TIMEOUT,
            params={"per_page": 10},
        )
        if resp.status_code != 200:
            logger.warning("GitHub releases %s returned %d", tool["repo"], resp.status_code)
            return out
        for rel in resp.json():
            published = rel.get("published_at") or ""
            if not published:
                continue
            try:
                pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                continue
            if pub_dt < cutoff_dt:  # type: ignore[operator]
                continue
            tag = rel.get("tag_name", "")
            body = (rel.get("body") or "")[:2000]
            out.append(
                {
                    "tool": tool["name"],
                    "kind": "release",
                    "title": f"{tool['name']} {tag}",
                    "summary": body,
                    "url": rel.get("html_url", ""),
                    "published_iso": published,
                    "source": f"GitHub Releases ({tool['repo']})",
                    "_tag": tag,
                    "_is_prerelease": bool(rel.get("prerelease")),
                }
            )

        # Pre-release dedup: group consecutive alpha/RC/dev tags sharing
        # a base version, keep the latest per group. Iterate latest-
        # first so the surviving entry is the most-recent pre-release.
        deduped: list[dict] = []
        seen_bases: set[str] = set()
        out.sort(key=lambda r: r.get("published_iso", ""), reverse=True)
        for rel in out:
            tag = rel.get("_tag", "")
            if rel.get("_is_prerelease") or any(t in tag.lower() for t in ("alpha", "beta", "rc", "-dev")):
                base = re.split(r"-(?:alpha|beta|rc|dev)", tag, maxsplit=1)[0]
                if base in seen_bases:
                    continue
                seen_bases.add(base)
            deduped.append(rel)
        for r in deduped:
            r.pop("_tag", None)
            r.pop("_is_prerelease", None)
        return deduped
    except Exception as e:
        logger.debug("GitHub releases fetch failed for %s: %s", tool["repo"], e)
    return out


def _fetch_github_commits_recent(tool: dict, cutoff_dt: object) -> list[dict]:
    """Recent commits to the tool's default branch.

    Includes commits in the same window as releases. A burst of commits
    immediately before a release tag often signals shipping activity that
    won't show up in the release tag for another few hours. Capped at
    5 commits per tool to keep the prompt focused.
    """
    out: list[dict] = []
    try:
        # ``since`` accepts ISO 8601 — pass the cutoff directly so the
        # API does the filtering server-side.
        since_iso = cutoff_dt.isoformat().replace("+00:00", "Z")  # type: ignore[attr-defined]
        resp = requests.get(
            f"https://api.github.com/repos/{tool['repo']}/commits",
            headers={**_HEADERS, "Accept": "application/vnd.github.v3+json"},
            timeout=_TIMEOUT,
            params={"per_page": 5, "since": since_iso},
        )
        if resp.status_code != 200:
            logger.warning("GitHub commits %s returned %d", tool["repo"], resp.status_code)
            return out
        for c in resp.json():
            sha = (c.get("sha") or "")[:7]
            msg_full = (c.get("commit") or {}).get("message", "")
            # Take the subject line only — body is usually noisy
            # (Co-Authored-By, references). Truncate aggressively.
            msg = msg_full.split("\n", 1)[0][:200]
            published = (c.get("commit") or {}).get("author", {}).get("date") or ""
            out.append(
                {
                    "tool": tool["name"],
                    "kind": "commit",
                    "title": f"{tool['name']} commit {sha}: {msg}",
                    "summary": msg_full[:500],
                    "url": c.get("html_url", ""),
                    "published_iso": published,
                    "source": f"GitHub Commits ({tool['repo']})",
                }
            )
    except Exception as e:
        logger.debug("GitHub commits fetch failed for %s: %s", tool["repo"], e)
    return out


_BLOG_DATE_RE = re.compile(
    r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+20\d{2})",
    re.IGNORECASE,
)


def _pretty_published(raw: str) -> str:
    """Render a published timestamp for the prompt block.

    Handles both ISO 8601 ("2026-05-12T00:27:00+00:00") and RFC 2822
    ("Thu, 07 May 2026 12:00:00 GMT") inputs uniformly. ISO 8601 gets
    the T-separator collapsed to a space; RFC 2822 is returned as-is
    after a length trim. The previous code did a global ``.replace
    ('T', ' ')`` which munched the T in 'Thu' and emitted ' hu, 07
    May 2026' — pin the right format here so the bug stays dead.
    """
    if not raw:
        return ""
    s = raw.strip()
    # ISO 8601: starts with 4 digits + dash. Safely T->space.
    if len(s) >= 10 and s[:4].isdigit() and s[4] == "-":
        return f"{s[:16].replace('T', ' ')} UTC"
    return f"{s[:32]} UTC"


def _parse_blog_date(text: str) -> object:
    """Best-effort parse of an embedded date string like 'May 6, 2026'.

    Returns a tz-aware datetime when matched, else None. Anthropic's
    news page bakes the date into the link text alongside the title
    ("AnnouncementsMay 6, 2026Higher usage limits for Claude..."),
    so this lets us cutoff-filter blog entries without depending on a
    `<time datetime="">` attribute we'd have to scrape separately.
    """
    if not text:
        return None
    m = _BLOG_DATE_RE.search(text)
    if not m:
        return None
    from datetime import UTC, datetime

    for fmt in ("%b %d, %Y", "%b %d %Y", "%B %d, %Y", "%B %d %Y"):
        try:
            dt = datetime.strptime(m.group(1).replace(".", ""), fmt)
            return dt.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _fetch_vendor_blog_recent(tool: dict, cutoff_dt: object) -> list[dict]:
    """Vendor blog posts mentioning the tool, filtered to the window.

    Anthropic / Google paths now restrict to ``/news/`` anchor hrefs so
    nav links to product / pricing pages (which match the tool keyword
    by accident) don't bleed in. Date parsing is best-effort via
    ``_parse_blog_date``: when we can extract a date from the link text
    we honour the cutoff window; when we can't, we include the entry
    anyway because most vendor news pages list latest-first.
    """
    out: list[dict] = []
    try:
        resp = requests.get(tool["blog_url"], headers=_HEADERS, timeout=_TIMEOUT)
        if resp.status_code != 200:
            return out
        soup = BeautifulSoup(resp.text, "xml" if tool["blog_url"].endswith(".xml") else "html.parser")

        if tool["blog_url"].endswith(".xml"):
            # RSS path (OpenAI). pubDate is RFC 2822 ("Thu, 07 May 2026
            # 12:00:00 GMT"); parse it for the cutoff filter and pass
            # through as-is for display. The display code MUST NOT
            # blindly .replace('T', ' ') on this — that ate the T in
            # "Thu" and produced " hu, 07 May" in earlier output.
            from datetime import UTC, datetime
            from email.utils import parsedate_to_datetime

            items = soup.find_all("item")[:15]
            for it in items:
                title_el = it.find("title")
                link_el = it.find("link")
                date_el = it.find("pubDate") or it.find("dc:date")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                if not _matches_keyword(title, tool["blog_filter_keywords"]):
                    continue
                pub_str = date_el.get_text(strip=True) if date_el else ""
                pub_dt: object = None
                if pub_str:
                    try:
                        pub_dt = parsedate_to_datetime(pub_str)
                        if pub_dt.tzinfo is None:  # type: ignore[attr-defined]
                            pub_dt = pub_dt.replace(tzinfo=UTC)  # type: ignore[union-attr]
                    except (TypeError, ValueError):
                        pub_dt = None
                if pub_dt is not None and pub_dt < cutoff_dt:  # type: ignore[operator]
                    continue
                # Normalise published_iso to ISO 8601 when we parsed it,
                # so the downstream display ("[:16].replace('T',' ')")
                # works uniformly.
                iso = pub_dt.isoformat() if isinstance(pub_dt, datetime) else pub_str
                out.append(
                    {
                        "tool": tool["name"],
                        "kind": "blog",
                        "title": f"{tool['blog_source_label']}: {title}",
                        "summary": "",
                        "url": link_el.get_text(strip=True) if link_el else "",
                        "published_iso": iso,
                        "source": tool["blog_source_label"],
                    }
                )
        else:
            # HTML scrape — Anthropic / Google. Restrict to /news/-style
            # hrefs to skip nav / product links, parse embedded date.
            seen: set[str] = set()
            news_selectors = ["a[href^='/news/']", "a[href^='/blog/']", "a[href*='/news/']"]
            anchors: list = []
            for sel in news_selectors:
                anchors.extend(soup.select(sel))
            if not anchors:
                # Fallback — keep prior behaviour but at least restrict
                # to in-domain hrefs so product pages on other domains
                # don't slip through.
                anchors = [a for a in soup.select("a") if (a.get("href") or "").startswith(("/", tool["blog_url"]))]

            for a in anchors:
                title = a.get_text(strip=True)
                href = a.get("href", "")
                # Drop href self-references (/news/, /news/category/...).
                if href.rstrip("/").endswith(("/news", "/blog")) or "/category/" in href:
                    continue
                if len(title) < 20 or title in seen:
                    continue
                if not _matches_keyword(title, tool["blog_filter_keywords"]):
                    continue
                blog_dt = _parse_blog_date(title)
                if blog_dt is not None and blog_dt < cutoff_dt:  # type: ignore[operator]
                    continue
                seen.add(title)
                if href.startswith("/"):
                    from urllib.parse import urlparse

                    parsed = urlparse(tool["blog_url"])
                    href = f"{parsed.scheme}://{parsed.netloc}{href}"
                iso_str = blog_dt.isoformat() if blog_dt is not None else ""  # type: ignore[union-attr]
                out.append(
                    {
                        "tool": tool["name"],
                        "kind": "blog",
                        "title": f"{tool['blog_source_label']}: {title}",
                        "summary": "",
                        "url": href,
                        "published_iso": iso_str,
                        "source": tool["blog_source_label"],
                    }
                )
                if len(seen) >= 3:
                    break
    except Exception as e:
        logger.debug("Vendor blog fetch failed for %s: %s", tool["blog_url"], e)
    return out


def _matches_keyword(text: str, keywords: list[str]) -> bool:
    """Case-insensitive substring match against any of the tool keywords."""
    if not text:
        return False
    lower = text.lower()
    return any(k.lower() in lower for k in keywords)


def fetch_hn_ai_buzz(min_score: int = 50, min_comments: int = 30, max_items: int = 10) -> list[dict]:
    """High-engagement AI stories on Hacker News — proxy for "what's
    AI Twitter buzzing about".

    Stricter filter than ``fetch_hackernews_ai``: requires score >=
    ``min_score`` OR comments >= ``min_comments`` so we only surface
    posts that have actually broken out. HN's AI subset tends to lag
    Twitter by a few hours but the bar there is much higher signal-
    to-noise than raw Twitter (no paid API access for X anyway).
    """
    try:
        resp = requests.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json",
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return []
        story_ids = resp.json()[:150]
        out: list[dict] = []
        for story_id in story_ids:
            if len(out) >= max_items:
                break
            try:
                r2 = requests.get(f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json", timeout=_TIMEOUT)
                if r2.status_code != 200:
                    continue
                item = r2.json()
                if not item or item.get("type") != "story":
                    continue
                score = int(item.get("score", 0))
                comments = int(item.get("descendants", 0))
                if score < min_score and comments < min_comments:
                    continue
                title = item.get("title", "")
                if not _is_ai_related(title):
                    continue
                out.append(
                    {
                        "title": title,
                        "url": item.get("url") or f"https://news.ycombinator.com/item?id={story_id}",
                        "score": score,
                        "comments": comments,
                        "source": "Hacker News",
                    }
                )
            except Exception:
                continue
        logger.info("fetch_hn_ai_buzz: %d items (score>=%d or comments>=%d)", len(out), min_score, min_comments)
        return out
    except Exception as e:
        logger.warning("HN buzz fetch failed: %s", e)
        return []


def fetch_github_trending_buzz(max_items: int = 10) -> list[dict]:
    """GitHub Trending (daily) filtered for AI keywords — directly
    "what's getting starred today in AI".

    Identical mechanic to ``fetch_github_trending_ai`` but exposed as a
    separate name so phase_gather can mark its output as a "buzz"
    section in the prompt distinct from tool-specific updates.
    """
    return fetch_github_trending_ai(max_items=max_items)


_RSSHUB_MIRRORS = [
    "https://rsshub.app",
    "https://rsshub.atgw.io",
    "https://rss.shab.fun",
    "https://rsshub.rssforever.com",
    "https://rsshub.feeded.xyz",
]


def fetch_x_ai_buzz(max_items: int = 10) -> list[dict]:
    """Best-effort X / Twitter signal via RSSHub public mirrors.

    X has no free programmatic access tier; we try a small set of
    public RSSHub mirrors in order and take the first one that
    responds. Every mirror is flaky in its own way (rate-limited,
    cookie-locked, occasionally down) so the fallback chain is what
    keeps this source useful — when *all* mirrors fail we log once
    and return [], and the caller treats absence as "no Twitter
    buzz this run". HN / Reddit / GitHub Trending cover most of the
    real buzz independently.

    Accounts chosen for high signal per post (rather than volume) —
    Anthropic / OpenAI / Google official + engineering voices who
    consistently surface AI tool news first.
    """
    accounts = [
        "AnthropicAI",
        "OpenAI",
        "GoogleAI",
        "GoogleDeepMind",
        "simonw",  # Simon Willison — frequent Claude Code / LLM dev commentary
        "swyx",
        "alexalbert__",  # Anthropic developer relations
    ]

    # Pick a working RSSHub mirror with one probe request. Anything
    # else is wasted retries; if the chosen mirror later fails on a
    # specific account we just skip that account.
    working_mirror = None
    for mirror in _RSSHUB_MIRRORS:
        try:
            probe = requests.get(
                f"{mirror}/twitter/user/{accounts[0]}",
                headers=_HEADERS,
                timeout=6,
            )
            if probe.status_code == 200 and "<item" in probe.text:
                working_mirror = mirror
                break
        except Exception:
            continue
    if not working_mirror:
        logger.info("fetch_x_ai_buzz: 0 items — no RSSHub mirror responded with feed data")
        return []

    out: list[dict] = []
    per_request_timeout = 6
    # Walk remaining accounts on the chosen mirror. Don't re-probe;
    # whatever fails fails.
    for account in accounts:
        if len(out) >= max_items:
            break
        try:
            resp = requests.get(
                f"{working_mirror}/twitter/user/{account}",
                headers=_HEADERS,
                timeout=per_request_timeout,
            )
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "xml")
            for item in soup.find_all("item")[:3]:  # latest 3 per account
                title_el = item.find("title")
                link_el = item.find("link")
                date_el = item.find("pubDate")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                # AI-relevance gate even on AI accounts — they post
                # plenty of non-AI stuff (recruiting, ops, etc.).
                if not _is_ai_related(title):
                    continue
                out.append(
                    {
                        "title": f"@{account}: {title[:200]}",
                        "url": link_el.get_text(strip=True) if link_el else "",
                        "published": date_el.get_text(strip=True) if date_el else "",
                        "source": f"X / @{account}",
                    }
                )
                if len(out) >= max_items:
                    break
        except Exception:
            continue
    if out:
        logger.info("fetch_x_ai_buzz: %d items via %s", len(out), working_mirror)
    else:
        logger.info("fetch_x_ai_buzz: 0 items (mirror responded to probe but no AI-tagged posts)")
    return out


def fetch_reddit_ai_buzz(
    max_items: int = 12,
    per_sub_cap: int = 3,
    min_score: int = 100,
    min_comments: int = 30,
) -> list[dict]:
    """High-karma AI community discussions from Reddit JSON API.

    Six subreddits where Claude / Codex / Gemini conversations actually
    happen, hit via Reddit's free JSON top-of-day endpoint. Per-sub
    cap forces diversity: without it, r/ClaudeAI alone tends to
    dominate the buzz section with memes and personal anecdotes (it's
    the most active sub by volume), drowning out actual technical
    discussion in r/LocalLLaMA / r/MachineLearning / r/Bard.

    Endpoint choice: ``top.json?t=day`` instead of ``hot.json``. Hot
    blends recency into the score; top-of-day surfaces only the
    posts the community actually upvoted hard in the last 24 hours,
    which trims meme posts that get there on novelty alone.

    Filters:
    - score >= ``min_score`` OR num_comments >= ``min_comments``
    - title passes ``_is_ai_related`` (defensive — sub is AI-themed)
    - skip stickied / NSFW posts
    - per-sub cap ensures source diversity
    """
    subs = [
        "ClaudeAI",
        "OpenAI",
        "LocalLLaMA",
        "MachineLearning",
        "Bard",  # Gemini discussions live here
        "singularity",
    ]
    out: list[dict] = []
    headers = {
        **_HEADERS,
        # Reddit blocks generic UA strings. A descriptive one stays
        # under the radar and is honest.
        "User-Agent": "ai-workflows/0.1 (tech_catchup digest, github.com/dumbled0re/ai-workflows)",
    }
    for sub in subs:
        if len(out) >= max_items:
            break
        try:
            resp = requests.get(
                f"https://www.reddit.com/r/{sub}/top.json",
                headers=headers,
                params={"limit": 15, "t": "day"},
                timeout=10,
            )
            if resp.status_code != 200:
                logger.debug("reddit %s returned %d", sub, resp.status_code)
                continue
            data = resp.json()
            sub_taken = 0
            for child in data.get("data", {}).get("children", []):
                if sub_taken >= per_sub_cap:
                    break
                if len(out) >= max_items:
                    break
                post = child.get("data", {})
                if post.get("stickied") or post.get("over_18"):
                    continue
                score = int(post.get("score", 0))
                comments = int(post.get("num_comments", 0))
                if score < min_score and comments < min_comments:
                    continue
                title = post.get("title", "")
                if not _is_ai_related(title):
                    continue
                permalink = post.get("permalink", "")
                out.append(
                    {
                        "title": title,
                        "url": f"https://www.reddit.com{permalink}" if permalink else (post.get("url") or ""),
                        "score": score,
                        "comments": comments,
                        "source": f"r/{sub}",
                    }
                )
                sub_taken += 1
        except Exception as e:
            logger.debug("reddit fetch failed for r/%s: %s", sub, e)
            continue
    logger.info(
        "fetch_reddit_ai_buzz: %d items (per-sub cap=%d, min_score=%d) across %d subs",
        len(out),
        per_sub_cap,
        min_score,
        len(subs),
    )
    return out


def format_buzz_layer(
    hn: list[dict],
    trending: list[dict],
    x_buzz: list[dict],
    reddit: list[dict] | None = None,
) -> str:
    """Render the AI-buzz layer (HN + Trending + X + Reddit) as a
    prompt block distinct from the focused tool updates."""
    reddit = reddit or []
    if not hn and not trending and not x_buzz and not reddit:
        return ""
    parts: list[str] = []
    if hn:
        parts.append("\n=== Hacker News (高エンゲージメント AI ストーリー) ===")
        for item in hn:
            parts.append(
                f"- [{item.get('score', 0)}pts, {item.get('comments', 0)}comments] {item.get('title', '')}\n"
                f"  URL: {item.get('url', '')}"
            )
    if reddit:
        parts.append("\n=== Reddit (AI subreddit ホット) ===")
        for item in reddit:
            parts.append(
                f"- [{item.get('source', '')}, {item.get('score', 0)}pts, "
                f"{item.get('comments', 0)}comments] {item.get('title', '')}\n"
                f"  URL: {item.get('url', '')}"
            )
    if trending:
        parts.append("\n=== GitHub Trending (AI 関連 — daily) ===")
        for repo in trending:
            lang = f" [{repo['language']}]" if repo.get("language") else ""
            stars = f" ({repo['stars_today']})" if repo.get("stars_today") else ""
            desc = repo.get("description", "")
            url = repo.get("url", "")
            parts.append(f"- {repo['name']}{lang}{stars}\n  {desc}\n  URL: {url}")
    if x_buzz:
        parts.append("\n=== X (Twitter) — AI 系アカウントの最新投稿 ===")
        for item in x_buzz:
            parts.append(f"- [{item.get('source', '')}] {item.get('title', '')}\n  URL: {item.get('url', '')}")
    return "\n".join(parts)


def format_focused_updates(updates: list[dict]) -> str:
    """Render the focused-updates list into a prompt-injection string.

    Groups by tool so the AI sees Claude Code / Codex / Gemini CLI as
    distinct sections rather than one chronological mush. Within each
    tool group, items are ordered release → commit → blog so the most
    canonical / version-stable info leads.
    """
    if not updates:
        return ""
    by_tool: dict[str, list[dict]] = {}
    for u in updates:
        by_tool.setdefault(u["tool"], []).append(u)
    kind_order = {"release": 0, "commit": 1, "blog": 2}
    parts: list[str] = []
    for tool, items in by_tool.items():
        parts.append(f"\n=== {tool} ===")
        items_sorted = sorted(items, key=lambda x: (kind_order.get(x.get("kind", "blog"), 9), -1))
        for u in items_sorted:
            kind = u.get("kind", "blog")
            published = u.get("published_iso", "") or "(時刻不明)"
            line = f"- [{kind.upper()}] {u.get('title', '')}"
            if published:
                line += f"  ({_pretty_published(published)})"
            parts.append(line)
            summary = (u.get("summary") or "").strip()
            if summary:
                # Indent multi-line summary for readability
                summary_short = summary[:1500]
                parts.append(f"  概要: {summary_short}")
            url = u.get("url", "")
            if url:
                parts.append(f"  URL: {url}")
    return "\n".join(parts)


def format_all_sources(
    hn: list[dict],
    github: list[dict],
    arxiv: list[dict],
    company_news: list[dict] | None = None,
    tool_releases: list[dict] | None = None,
) -> str:
    """Format all sources into a single prompt for Claude to analyze."""
    parts: list[str] = []

    if hn:
        parts.append("=== Hacker News (AI関連トップストーリー) ===")
        for item in hn:
            parts.append(f"- [{item['score']}pts, {item['comments']}comments] {item['title']}\n  URL: {item['url']}")

    if github:
        parts.append("\n=== GitHub Trending (AI関連リポジトリ) ===")
        for repo in github:
            lang = f" [{repo['language']}]" if repo.get("language") else ""
            stars = f" ({repo['stars_today']})" if repo.get("stars_today") else ""
            parts.append(f"- {repo['name']}{lang}{stars}\n  {repo['description']}\n  URL: {repo['url']}")

    if arxiv:
        parts.append("\n=== arXiv (最新AI/ML論文) ===")
        for paper in arxiv:
            parts.append(
                f"- {paper['title']}\n  著者: {paper['authors']}\n  概要: {paper['summary']}\n  URL: {paper['url']}"
            )

    if company_news:
        parts.append("\n=== AI企業公式ニュース (Anthropic / OpenAI / Google) ===")
        for item in company_news:
            parts.append(f"- [{item['source']}] {item['title']}\n  URL: {item['url']}")

    if tool_releases:
        # Split priority releases into a separate must-cover section so the
        # analyzer reliably surfaces them even when the day's news is dense.
        priority_items = [r for r in tool_releases if r.get("priority")]
        other_items = [r for r in tool_releases if not r.get("priority")]

        if priority_items:
            parts.append("\n=== 【必須掲載】Claude Code / Codex / Gemini CLI 等の最新リリース ===")
            parts.append(
                "※ これらは利用ツールの中核であり、毎回 digest に必ず1項目以上掲載すること。"
                "リリース内容の新機能・破壊的変更・バグ修正を簡潔に要約。"
            )
            for item in priority_items:
                parts.append(
                    f"- {item['title']} ({item.get('published', '')})\n"
                    f"  {item.get('changelog', '')}\n"
                    f"  URL: {item['url']}"
                )

        if other_items:
            parts.append("\n=== AIツール最新リリース（その他） ===")
            for item in other_items:
                parts.append(
                    f"- {item['title']} ({item.get('published', '')})\n"
                    f"  {item.get('changelog', '')}\n"
                    f"  URL: {item['url']}"
                )

    return "\n".join(parts)
