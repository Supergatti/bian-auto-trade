import time
import requests

from config import DEEPSEEK_URL, DEEPSEEK_API_KEY, MODEL_PRO, MODEL_FLASH, logger


def _call_deepseek(messages, model=MODEL_FLASH, temperature=0.3, max_tokens=4096, timeout=120, max_retries=3):
    last_err = None
    for attempt in range(max_retries):
        try:
            return _do_call(messages, model, temperature, max_tokens, timeout)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_err = e
            wait = (attempt + 1) * 3
            logger.warning("DeepSeek %s 连接失败 (attempt %d/%d), %ds 后重试: %s",
                         model, attempt + 1, max_retries, wait, str(e)[:80])
            time.sleep(wait)
        except RuntimeError as e:
            msg = str(e)
            if "429" in msg or "rate" in msg.lower():
                last_err = e
                wait = (attempt + 1) * 5
                logger.warning("DeepSeek %s rate limited (attempt %d/%d), %ds 后重试",
                             model, attempt + 1, max_retries, wait)
                time.sleep(wait)
            elif "500" in msg or "502" in msg or "503" in msg:
                last_err = e
                wait = (attempt + 1) * 2
                logger.warning("DeepSeek %s server error (attempt %d/%d), %ds 后重试: %s",
                             model, attempt + 1, max_retries, wait, msg[:80])
                time.sleep(wait)
            else:
                raise
    raise last_err


def _do_call(messages, model, temperature, max_tokens, timeout):
    start = time.time()
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
