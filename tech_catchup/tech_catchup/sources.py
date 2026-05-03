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
