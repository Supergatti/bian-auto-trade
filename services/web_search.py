import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import TAVILY_API_KEY, logger


def _search_tavily(query, num_results=5):
    """Search via Tavily API (AI-optimized search)."""
    results = []
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "search_depth": "advanced",
                "max_results": num_results,
                "include_answer": True,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning("Tavily returned %d: %s", resp.status_code, resp.text[:200])
            return results

        data = resp.json()

        # Include the AI-generated answer if available
        answer = data.get("answer", "")
        if answer:
            results.append({
                "title": f"AI Summary: {query}",
                "snippet": answer[:500],
                "url": "",
                "source": "tavily_answer",
            })

        for r in data.get("results", []):
            results.append({
                "title": r.get("title", ""),
                "snippet": (r.get("content", "") or r.get("snippet", ""))[:400],
                "url": r.get("url", ""),
                "source": "tavily",
            })
    except Exception as e:
        logger.warning("Tavily search failed: %s", str(e)[:80])

    return results


def _search_duckduckgo(query, num_results=5):
    """Minimal DuckDuckGo fallback."""
    results = []
    try:
        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            timeout=12,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        snippets = re.findall(r'class="result__snippet">(.*?)</a>', resp.text, re.DOTALL)
        titles = re.findall(r'class="result__title".*?<a[^>]*>(.*?)</a>', resp.text, re.DOTALL)
        links = re.findall(r'class="result__title".*?href="([^"]+)"', resp.text)
        for i in range(min(num_results, len(titles))):
            snip = re.sub(r'<[^>]+>', '', snippets[i] if i < len(snippets) else "").strip()
            title = re.sub(r'<[^>]+>', '', titles[i] if i < len(titles) else "").strip()
            link = links[i] if i < len(links) else ""
            if title and len(snip) > 20:
                results.append({"title": title, "snippet": snip[:300], "url": link, "source": "ddg"})
    except Exception as e:
        logger.debug("DDG fallback failed: %s", str(e)[:60])
    return results


def search_web(query, num_results=5):
    """Search via Tavily (primary) with DuckDuckGo HTML fallback."""
    all_results = []
    seen_urls = set()

    # Primary: Tavily
    if TAVILY_API_KEY:
        tavily_results = _search_tavily(query, num_results)
        for r in tavily_results:
            url = r.get("url", "")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            all_results.append(r)
        logger.info("🌐 Tavily: '%s' → %d 条结果", query, len(tavily_results))

    # Fallback: DuckDuckGo
    if len(all_results) < 3:
        ddg_results = _search_duckduckgo(query, num_results)
        for r in ddg_results:
            url = r.get("url", "")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            all_results.append(r)

    # Deduplicate by title
    unique = []
    seen_titles = set()
    for r in all_results:
        t = r.get("title", "").strip().lower()
        if t and t not in seen_titles and len(r.get("snippet", "")) > 15:
            seen_titles.add(t)
            unique.append(r)

    logger.info("🌐 搜索 '%s' → %d 条结果", query, len(unique))
    return unique
