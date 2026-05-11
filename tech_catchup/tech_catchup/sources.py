from __future__ import annotations

import logging

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
_TIMEOUT = 15

# AI-related keywords for filtering
_AI_KEYWORDS = [
    "ai",
    "artificial intelligence",
    "machine learning",
    "deep learning",
    "llm",
    "large language model",
    "gpt",
    "claude",
    "gemini",
    "openai",
    "anthropic",
    "transformer",
    "neural",
    "nlp",
    "computer vision",
    "diffusion",
    "generative",
    "agent",
    "rag",
    "fine-tune",
    "embedding",
    "chatbot",
    "copilot",
    "model",
    "inference",
    "training",
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
    "mcp",
    "tool use",
]


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

                title = item.get("title", "").lower()
                if any(kw in title for kw in _AI_KEYWORDS):
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

            # Check if AI-related
            combined = f"{repo_name} {desc}".lower()
            if not any(kw in combined for kw in _AI_KEYWORDS):
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
    """GitHub releases for one tool, filtered to those after ``cutoff_dt``."""
    from datetime import datetime

    out: list[dict] = []
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{tool['repo']}/releases",
            headers={**_HEADERS, "Accept": "application/vnd.github.v3+json"},
            timeout=_TIMEOUT,
            params={"per_page": 5},
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
                }
            )
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


def _fetch_vendor_blog_recent(tool: dict, cutoff_dt: object) -> list[dict]:
    """Vendor blog posts mentioning the tool, filtered to the window.

    Cutoff filtering here is best-effort: blogs don't always expose
    machine-parseable dates in HTML. When the date is missing we
    include the post anyway (latest item) so we don't miss
    announcement blogs that happen to land outside the cron's exact
    window. The AI summariser dedupes against the GitHub release/
    commit content downstream.
    """
    out: list[dict] = []
    try:
        resp = requests.get(tool["blog_url"], headers=_HEADERS, timeout=_TIMEOUT)
        if resp.status_code != 200:
            return out
        soup = BeautifulSoup(resp.text, "xml" if tool["blog_url"].endswith(".xml") else "html.parser")

        if tool["blog_url"].endswith(".xml"):
            # RSS path (OpenAI)
            items = soup.find_all("item")[:10]
            for it in items:
                title_el = it.find("title")
                link_el = it.find("link")
                date_el = it.find("pubDate") or it.find("dc:date")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                if not _matches_keyword(title, tool["blog_filter_keywords"]):
                    continue
                out.append(
                    {
                        "tool": tool["name"],
                        "kind": "blog",
                        "title": f"{tool['blog_source_label']}: {title}",
                        "summary": "",
                        "url": link_el.get_text(strip=True) if link_el else "",
                        "published_iso": date_el.get_text(strip=True) if date_el else "",
                        "source": tool["blog_source_label"],
                    }
                )
        else:
            # HTML scrape — Anthropic / Google. Anchor-tag heuristic.
            seen: set[str] = set()
            for a in soup.select("a"):
                title = a.get_text(strip=True)
                href = a.get("href", "")
                if len(title) < 20 or title in seen:
                    continue
                if not _matches_keyword(title, tool["blog_filter_keywords"]):
                    continue
                seen.add(title)
                if href.startswith("/"):
                    # reconstruct absolute URL from blog_url's host
                    from urllib.parse import urlparse

                    parsed = urlparse(tool["blog_url"])
                    href = f"{parsed.scheme}://{parsed.netloc}{href}"
                out.append(
                    {
                        "tool": tool["name"],
                        "kind": "blog",
                        "title": f"{tool['blog_source_label']}: {title}",
                        "summary": "",
                        "url": href,
                        "published_iso": "",
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
                if not any(kw in title.lower() for kw in _AI_KEYWORDS):
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


def fetch_x_ai_buzz(max_items: int = 10) -> list[dict]:
    """Best-effort X / Twitter signal via RSSHub public mirrors.

    X has no free programmatic access tier, so we rely on RSSHub's
    bridge for a handful of high-signal AI accounts. RSSHub mirrors
    are flaky and rate-limited; each request silently fails to an
    empty result on timeout or non-200, and the caller treats absence
    of data as "no Twitter buzz this run". This is acceptable: HN
    and GitHub Trending cover most of the real buzz independently.

    Accounts chosen for high signal per post (rather than volume) —
    Anthropic / OpenAI / Google official + a few engineering voices
    who consistently surface AI tool news first.
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
    out: list[dict] = []
    # Trim per-account requests aggressively — even 1 successful
    # account is useful, and a slow RSSHub mirror shouldn't block
    # the rest of the gather pipeline.
    per_request_timeout = 5
    for account in accounts:
        if len(out) >= max_items:
            break
        try:
            resp = requests.get(
                f"https://rsshub.app/twitter/user/{account}",
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
                if not any(kw in title.lower() for kw in _AI_KEYWORDS):
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
            # RSSHub down / rate-limited / network hiccup — silently
            # skip this account. Don't log per-account because the
            # mirror dies daily and the noise drowns the run log.
            continue
    if out:
        logger.info("fetch_x_ai_buzz: %d items via RSSHub", len(out))
    else:
        logger.info("fetch_x_ai_buzz: 0 items (RSSHub unavailable or no AI-tagged posts)")
    return out


def format_buzz_layer(
    hn: list[dict],
    trending: list[dict],
    x_buzz: list[dict],
) -> str:
    """Render the AI-buzz layer (HN high-score + Trending + X) as a
    prompt block distinct from the focused tool updates."""
    if not hn and not trending and not x_buzz:
        return ""
    parts: list[str] = []
    if hn:
        parts.append("\n=== Hacker News (高エンゲージメント AI ストーリー) ===")
        for item in hn:
            parts.append(
                f"- [{item.get('score', 0)}pts, {item.get('comments', 0)}comments] {item.get('title', '')}\n"
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
                line += f"  ({published[:16].replace('T', ' ')} UTC)"
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
