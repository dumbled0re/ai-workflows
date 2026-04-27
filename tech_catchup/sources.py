from __future__ import annotations

import logging
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}
_TIMEOUT = 15

# AI-related keywords for filtering
_AI_KEYWORDS = [
    "ai", "artificial intelligence", "machine learning", "deep learning",
    "llm", "large language model", "gpt", "claude", "gemini", "openai",
    "anthropic", "transformer", "neural", "nlp", "computer vision",
    "diffusion", "generative", "agent", "rag", "fine-tune", "embedding",
    "chatbot", "copilot", "model", "inference", "training", "pytorch",
    "tensorflow", "hugging face", "langchain", "vector database",
    "prompt", "reasoning", "multimodal", "foundation model",
    "reinforcement learning", "mcp", "tool use",
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
                    ai_stories.append({
                        "title": item.get("title", ""),
                        "url": item.get("url", f"https://news.ycombinator.com/item?id={story_id}"),
                        "score": item.get("score", 0),
                        "comments": item.get("descendants", 0),
                        "source": "Hacker News",
                    })
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

            repos.append({
                "name": repo_name,
                "description": desc[:200],
                "language": language,
                "stars_today": stars_today,
                "url": f"https://github.com/{repo_name}",
                "source": "GitHub Trending",
            })

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

            papers.append({
                "title": title.get_text(strip=True) if title else "",
                "summary": summary.get_text(strip=True)[:300] if summary else "",
                "authors": ", ".join(author_names),
                "url": link.get_text(strip=True) if link else "",
                "source": "arXiv",
            })

        logger.info("Fetched %d papers from arXiv", len(papers))
        return papers
    except Exception as e:
        logger.warning("Failed to fetch arXiv: %s", e)
        return []


def format_all_sources(hn: list[dict], github: list[dict], arxiv: list[dict]) -> str:
    """Format all sources into a single prompt for Claude to analyze."""
    parts: list[str] = []

    if hn:
        parts.append("=== Hacker News (AI関連トップストーリー) ===")
        for item in hn:
            parts.append(
                f"- [{item['score']}pts, {item['comments']}comments] {item['title']}\n"
                f"  URL: {item['url']}"
            )

    if github:
        parts.append("\n=== GitHub Trending (AI関連リポジトリ) ===")
        for repo in github:
            lang = f" [{repo['language']}]" if repo.get("language") else ""
            stars = f" ({repo['stars_today']})" if repo.get("stars_today") else ""
            parts.append(
                f"- {repo['name']}{lang}{stars}\n"
                f"  {repo['description']}\n"
                f"  URL: {repo['url']}"
            )

    if arxiv:
        parts.append("\n=== arXiv (最新AI/ML論文) ===")
        for paper in arxiv:
            parts.append(
                f"- {paper['title']}\n"
                f"  著者: {paper['authors']}\n"
                f"  概要: {paper['summary']}\n"
                f"  URL: {paper['url']}"
            )

    return "\n".join(parts)
