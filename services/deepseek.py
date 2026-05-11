import time
import requests

from config import DEEPSEEK_URL, DEEPSEEK_API_KEY, MODEL_PRO, MODEL_FLASH, logger


def _call_deepseek(messages, model=MODEL_FLASH, temperature=0.3, max_tokens=4096, timeout=120):
    start = time.time()
    try:
        resp = requests.post(
            DEEPSEEK_URL,
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=timeout,
        )
        ct = resp.headers.get("Content-Type", "")
        if "text/html" in ct:
            snippet = resp.text[:200]
            logger.error("DeepSeek returned HTML (status=%d): %s", resp.status_code, snippet)
            raise RuntimeError(f"DeepSeek API returned HTML (status {resp.status_code}). Please check your API key and network.")

        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        body = ""
        try:
            body = e.response.text[:300]
        except Exception:
            pass
        logger.error("DeepSeek HTTP error %s: %s", str(e), body)
        raise RuntimeError(f"DeepSeek error: HTTP {e.response.status_code if e.response else ''} - {body}")

    elapsed = time.time() - start
    data = resp.json()
    if "choices" not in data or not data["choices"]:
        logger.error("DeepSeek returned no choices: %s", str(data)[:300])
        raise RuntimeError("DeepSeek returned no choices")

    content = data["choices"][0]["message"]["content"]
    logger.info("🧠 %s → %d chars (%.1fs)", model, len(content), elapsed)
    return content


def ask_flash(messages, temperature=0.3, max_tokens=2048):
    return _call_deepseek(messages, model=MODEL_FLASH, temperature=temperature, max_tokens=max_tokens)


def ask_pro(messages, temperature=0.3, max_tokens=4096):
    return _call_deepseek(messages, model=MODEL_PRO, temperature=temperature, max_tokens=max_tokens)
