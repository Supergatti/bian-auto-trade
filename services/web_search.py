import re
import requests
from config import logger


def search_web(query, num_results=5):
    results = []
    try:
        resp = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            timeout=10,
        )
        data = resp.json()
        abstract = data.get("AbstractText", "")
        if abstract:
            results.append({"title": data.get("Heading", query), "snippet": abstract[:300], "url": data.get("AbstractURL", "")})
        for topic in data.get("RelatedTopics", [])[:num_results]:
            if isinstance(topic, dict):
                if "Text" in topic:
                    results.append({
                        "title": topic.get("FirstURL", "").split("/")[-1].replace("_", " "),
                        "snippet": topic.get("Text", "")[:300],
                        "url": topic.get("FirstURL", ""),
                    })
                elif "Topics" in topic:
                    for sub in topic.get("Topics", [])[:3]:
                        if "Text" in sub:
                            results.append({
                                "title": sub.get("FirstURL", "").split("/")[-1].replace("_", " "),
                                "snippet": sub.get("Text", "")[:300],
                                "url": sub.get("FirstURL", ""),
                            })
    except Exception as e:
        logger.warning("DDG instant API failed: %s", str(e))

    if not results:
        try:
            resp = requests.post("https://html.duckduckgo.com/html/", data={"q": query}, timeout=15)
            snippets = re.findall(r'class="result__snippet">(.*?)</a>', resp.text, re.DOTALL)
            titles = re.findall(r'class="result__title".*?<a[^>]*>(.*?)</a>', resp.text, re.DOTALL)
            links = re.findall(r'class="result__title".*?href="([^"]+)"', resp.text)
            for i in range(min(num_results, len(titles))):
                snip = re.sub(r'<[^>]+>', '', snippets[i] if i < len(snippets) else "").strip()
                title = re.sub(r'<[^>]+>', '', titles[i] if i < len(titles) else "").strip()
                link = links[i] if i < len(links) else ""
                if title:
                    results.append({"title": title, "snippet": snip[:300], "url": link})
        except Exception as e:
            logger.warning("DDG HTML fallback failed: %s", str(e))

    logger.info("🌐 搜索 '%s' → %d 条结果", query, len(results))
    return results
