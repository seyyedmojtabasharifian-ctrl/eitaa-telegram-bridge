
import os
import re
import json
import time
import hashlib
import logging
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("eitaa-bridge")

SOURCE_URL = os.getenv("EITAA_URL", "").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [
    x.strip()
    for x in os.getenv("TELEGRAM_CHAT_IDS", "").split(",")
    if x.strip()
]

STATE_PATH = Path("data/seen.json")
MAX_MESSAGES = 50
TIMEOUT = 30

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Mobile Safari/537.36"
    ),
    "Accept-Language": "fa,en-US;q=0.9,en;q=0.8",
})


def clean_text(value):
    value = (value or "").replace("\xa0", " ")
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def get_page():
    if not SOURCE_URL:
        raise RuntimeError("Secret EITAA_URL is missing")

    response = SESSION.get(SOURCE_URL, timeout=TIMEOUT)

    log.info("Eitaa HTTP status: %s", response.status_code)
    log.info("Eitaa content type: %s",
             response.headers.get("Content-Type", ""))
    response.raise_for_status()

    response.encoding = response.apparent_encoding or "utf-8"
    html = response.text

    soup = BeautifulSoup(html, "html.parser")
    title = (
        clean_text(soup.title.get_text(" ", strip=True))
        if soup.title else "(no title)"
    )

    log.info("Eitaa HTML length: %d", len(html))
    log.info("Eitaa page title: %s", title)
    log.info("HTML preview: %s", clean_text(html[:500]))

    return html


def diagnose_html(html):
    """Print a small sample of text-bearing HTML elements."""
    soup = BeautifulSoup(html, "html.parser")
    log.info("=== HTML STRUCTURE DIAGNOSTIC ===")

    count = 0
    for node in soup.find_all(
        ["div", "section", "article", "a", "time"]
    ):
        text = clean_text(node.get_text(" ", strip=True))

        if len(text) < 100 or len(text) > 2500:
            continue

        attrs = {
            key: value
            for key, value in node.attrs.items()
            if key in (
                "class", "id", "data-post-id",
                "data-message-id", "href"
            )
        }

        log.info(
            "NODE tag=%s attrs=%s text=%s",
            node.name,
            str(attrs)[:250],
            text[:180]
        )

        count += 1
        if count >= 15:
            break

    log.info("Diagnostic nodes printed: %d", count)


def absolute_url(value):
    if not value:
        return ""

    url = urljoin(SOURCE_URL, value.strip())
    if urlparse(url).scheme not in ("http", "https"):
        return ""

    return url


def extract_media(node):
    media = []
    used = set()

    for tag in node.find_all(["img", "video", "source"]):
        src = None
        for attr in (
            "src", "data-src", "data-original",
            "data-lazy-src", "data-url"
        ):
            if tag.get(attr):
                src = tag.get(attr)
                break

        if src:
            url = absolute_url(src)
            if url and url not in used:
                media.append((
                    url, "photo" if tag.name == "img" else "video"
                ))
                used.add(url)

        if tag.name == "video" and tag.get("poster"):
            url = absolute_url(tag["poster"])
            if url and url not in used:
                media.append((url, "photo"))
                used.add(url)

    extensions = {
        ".jpg": "photo", ".jpeg": "photo",
        ".png": "photo", ".webp": "photo",
        ".gif": "photo", ".mp4": "video",
        ".mov": "video", ".m4v": "video",
        ".pdf": "document", ".zip": "document",
        ".rar": "document", ".doc": "document",
        ".docx": "document", ".xls": "document",
        ".xlsx": "document", ".ppt": "document",
        ".pptx": "document", ".mp3": "document",
        ".ogg": "document",
    }

    for link in node.find_all("a", href=True):
        url = absolute_url(link["href"])
        if not url or url in used:
            continue

        path = urlparse(url).path.lower()
        kind = next(
            (value for ext, value in extensions.items()
             if path.endswith(ext)),
            None
        )

        if kind:
            media.append((url, kind))
            used.add(url)

    return media[:10]


def extract_messages(html):
    soup = BeautifulSoup(html, "html.parser")
    nodes = []
    used_nodes = set()

    selectors = [
        "[data-post-id]",
        "[data-message-id]",
        "[data-id]",
        ".channel-post",
        ".post-item",
        ".post",
        ".message",
        ".feed-item",
        "article",
    ]

    for selector in selectors:
        for node in soup.select(selector):
            if id(node) not in used_nodes:
                nodes.append(node)
                used_nodes.add(id(node))

    log.info("HTML parse: found %d candidate containers", len(nodes))

    messages = []
    used_fingerprints = set()

    for node in nodes:
        text = clean_text(node.get_text("\n", strip=True))
        media = extract_media(node)

        if not text and not media:
            continue
        if len(text) > 15000:
            continue

        post_id = (
            node.get("data-post-id")
            or node.get("data-message-id")
            or node.get("data-id")
            or node.get("id")
            or ""
        )

        if not post_id:
            for link in node.find_all("a", href=True):
                match = re.search(
                    r"/([^/?#]+)/(\d+)/?$",
                    urlparse(link["href"]).path
                )
                if match:
                    post_id = match.group(2)
                    break

        media_urls = "\n".join(url for url, _ in media)
        fingerprint = hashlib.sha256(
            (str(post_id) + "\n" + text + "\n" + media_urls)
            .encode("utf-8")
        ).hexdigest()

        if fingerprint in used_fingerprints:
            continue
        used_fingerprints.add(fingerprint)

        messages.append({
            "id": str(post_id or fingerprint),
            "fingerprint": fingerprint,
            "text": text,
            "media": media,
        })

    messages = messages[:MAX_MESSAGES]
    messages.reverse()
    return messages


def load_state():
    if not STATE_PATH.exists():
        return None

    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return set(data.get("seen", []))
    except (OSError, ValueError):
        log.exception("Could not read state file")
        return set()


def save_state(seen):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_PATH.with_suffix(".tmp")

    temp.write_text(
        json.dumps(
            {"seen": sorted(seen)[-5000:]},
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )
    temp.replace(STATE_PATH)


def telegram(method, data=None, files=None):
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    response = SESSION.post(
        url, data=data or {}, files=files, timeout=60
    )
    response.raise_for_status()

    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram API error: {result.get('description', 'unknown')}"
        )

    return result["result"]


def send_text(chat_id, text):
    text = text or "(پیام بدون متن)"

    for start in range(0, len(text), 3900):
        telegram("sendMessage", {
            "chat_id": chat_id,
            "text": text[start:start + 3900],
            "disable_web_page_preview": "true",
        })


def send_media(chat_id, url, kind, caption=""):
    method = {
        "photo": "sendPhoto",
        "video": "sendVideo",
        "document": "sendDocument",
    }.get(kind, "sendDocument")

    field = {
        "photo": "photo",
        "video": "video",
        "document": "document",
    }.get(kind, "document")

    try:
        telegram(method, {
            "chat_id": chat_id,
            field: url,
            "caption": caption[:1000],
        })
        return True
    except Exception as exc:
        log.warning(
            "Direct media send failed: %s", type(exc).__name__
        )

    try:
        response = SESSION.get(
            url,
            timeout=TIMEOUT,
            headers={"Referer": SOURCE_URL}
        )
        response.raise_for_status()

        if "text/html" in response.headers.get(
            "Content-Type", ""
        ).lower():
            return False

        blob = response.content
        if not blob:
            return False

        filename = Path(urlparse(url).path).name or "media.bin"

        telegram(
            method,
            {"chat_id": chat_id, "caption": caption[:1000]},
            files={field: (filename, blob)}
        )
        return True

    except Exception as exc:
        log.warning(
            "Media download/upload failed: %s", type(exc).__name__
        )
        return False


def deliver(chat_id, message):
    text = message["text"]
    media = message["media"]

    if not media:
        send_text(chat_id, text)
        return

    caption = text
    any_sent = False

    for url, kind in media:
        if send_media(chat_id, url, kind, caption):
            any_sent = True
            caption = ""

    if not any_sent:
        send_text(
            chat_id,
            (text + "\n\n" if text else "")
            + "(انتقال رسانه ناموفق بود)"
        )
    elif caption:
        send_text(chat_id, caption)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Secret TELEGRAM_BOT_TOKEN is missing")
    if not CHAT_IDS:
        raise SystemExit("Secret TELEGRAM_CHAT_IDS is missing")

    html = get_page()

    # This diagnostic helps identify the real Eitaa HTML structure.
    diagnose_html(html)

    messages = extract_messages(html)
    log.info("Parsed %d candidate messages", len(messages))

    if not messages:
        raise SystemExit(
            "No messages detected. Share the NODE tag lines from "
            "the diagnostic output so the actual HTML can be analyzed."
        )

    seen = load_state()

    if seen is None:
        save_state({m["fingerprint"] for m in messages})
        log.info(
            "Initial baseline saved with %d items; nothing sent.",
            len(messages)
        )
        return

    for message in messages:
        key = message["fingerprint"]

        if key in seen:
            continue

        log.info("New item detected: %s", message["id"])
        success = True

        for chat_id in CHAT_IDS:
            try:
                deliver(chat_id, message)
                log.info("Delivery attempted for destination")
            except Exception:
                success = False
                log.exception("Destination delivery failed")

        if success:
            seen.add(key)
            save_state(seen)

        time.sleep(1)


if __name__ == "__main__":
    main()
