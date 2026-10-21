
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

SOURCE_URL = os.environ["EITAA_URL"].strip()
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
CHAT_IDS = [
    x.strip()
    for x in os.environ["TELEGRAM_CHAT_IDS"].split(",")
    if x.strip()
]

STATE_PATH = Path("data/seen.json")
MAX_MESSAGES = 50
TIMEOUT = 30

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13) "
        "AppleWebKit/537.36 Chrome/124.0 Mobile Safari/537.36"
    )
})


def clean_text(value):
    return re.sub(
        r"\n{3,}", "\n\n",
        (value or "").replace("\xa0", " ")
    ).strip()


def get_page():
    response = SESSION.get(SOURCE_URL, timeout=TIMEOUT)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def extract_media(node):
    media = []
    seen_urls = set()

    for tag in node.find_all(["img", "video", "source"]):
        src = (
            tag.get("src")
            or tag.get("data-src")
            or tag.get("data-original")
            or tag.get("data-lazy-src")
        )

        if not src and tag.name == "video":
            continue

        if src:
            url = urljoin(SOURCE_URL, src)
            if urlparse(url).scheme in ("http", "https"):
                kind = "photo" if tag.name == "img" else "video"
                if url not in seen_urls:
                    media.append((url, kind))
                    seen_urls.add(url)

        if tag.name == "video" and tag.get("poster"):
            url = urljoin(SOURCE_URL, tag["poster"])
            if url not in seen_urls:
                media.append((url, "photo"))
                seen_urls.add(url)

    for a in node.find_all("a", href=True):
        url = urljoin(SOURCE_URL, a["href"])
        path = urlparse(url).path.lower()

        extensions = {
            ".jpg": "photo", ".jpeg": "photo",
            ".png": "photo", ".webp": "photo",
            ".gif": "photo", ".mp4": "video",
            ".mov": "video", ".pdf": "document",
            ".zip": "document", ".rar": "document",
            ".doc": "document", ".docx": "document",
            ".xls": "document", ".xlsx": "document",
            ".ppt": "document", ".pptx": "document",
            ".mp3": "document", ".ogg": "document",
        }

        kind = next(
            (v for ext, v in extensions.items()
             if path.endswith(ext)),
            None
        )

        if kind and url not in seen_urls:
            media.append((url, kind))
            seen_urls.add(url)

    return media[:10]


def extract_messages(html):
    soup = BeautifulSoup(html, "html.parser")
    nodes = []
    seen_nodes = set()

    selectors = [
        "[data-post-id]",
        "[data-message-id]",
        ".channel-post",
        ".post-item",
        ".post",
        "article",
    ]

    for selector in selectors:
        for node in soup.select(selector):
            if id(node) not in seen_nodes:
                seen_nodes.add(id(node))
                nodes.append(node)

    messages = []
    fingerprints = set()

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
            or ""
        )

        if not post_id:
            for a in node.find_all("a", href=True):
                match = re.search(
                    r"/([^/?#]+)/(\d+)(?:[/?#]|$)",
                    urlparse(a["href"]).path
                )
                if match:
                    post_id = match.group(2)
                    break

        # Fallback identity when Eitaa exposes no post ID.
        identity = (
            str(post_id) if post_id else
            hashlib.sha256(
                (text + "\n" + "\n".join(u for u, _ in media))
                .encode("utf-8")
            ).hexdigest()
        )

        fingerprint = hashlib.sha256(
            (identity + "\n" + text + "\n" +
             "\n".join(u for u, _ in media)).encode("utf-8")
        ).hexdigest()

        if fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)

        messages.append({
            "id": identity,
            "fingerprint": fingerprint,
            "text": text,
            "media": media,
        })

    # Public pages commonly list newest items first.
    # Send older unseen items before newer ones.
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
        log.exception("State file could not be read")
        return set()


def save_state(seen):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(
        json.dumps({"seen": list(seen)[-5000:]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    temp.replace(STATE_PATH)


def telegram(method, data=None, files=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    response = SESSION.post(
        url, data=data or {}, files=files, timeout=60
    )
    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(str(result))

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

    # First let Telegram fetch the media URL itself.
    try:
        telegram(method, {
            "chat_id": chat_id,
            field: url,
            "caption": caption[:1000],
        })
        return True
    except Exception as exc:
        log.warning("Direct media transfer failed: %s", exc)

    # If the source requires a Referer, download and upload the file.
    try:
        response = SESSION.get(
            url, timeout=TIMEOUT, stream=True,
            headers={"Referer": SOURCE_URL}
        )
        response.raise_for_status()

        content_type = response.headers.get(
            "Content-Type", ""
        ).lower()

        if "text/html" in content_type:
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

    except Exception:
        log.exception("Media transfer failed")
        return False


def deliver(chat_id, message):
    text = message["text"]
    media = message["media"]

    if not media:
        send_text(chat_id, text)
        return

    first_caption = text
    sent_any = False

    for url, kind in media:
        success = send_media(
            chat_id, url, kind, first_caption
        )
        if success:
            sent_any = True
            first_caption = ""

    if not sent_any:
        send_text(
            chat_id,
            (text + "\n\n" if text else "") +
            "انتقال رسانه ناموفق بود."
        )
    elif first_caption and text:
        # Keep text if the first media failed but a later one worked.
        send_text(chat_id, text)


def main():
    if not BOT_TOKEN or not CHAT_IDS:
        raise SystemExit("Telegram secrets are missing.")

    messages = extract_messages(get_page())
    log.info("Parsed %d candidate messages", len(messages))

    if not messages:
        raise SystemExit(
            "No messages detected. Check the Eitaa page HTML/selectors."
        )

    seen = load_state()

    # First run establishes a baseline; it doesn't forward old posts.
    if seen is None:
        save_state({m["fingerprint"] for m in messages})
        log.info("Initial baseline saved; no old messages sent.")
        return

    for message in messages:
        key = message["fingerprint"]

        if key in seen:
            continue

        log.info("New item detected: %s", message["id"])

        # Mark only after attempting all destinations.
        for chat_id in CHAT_IDS:
            try:
                deliver(chat_id, message)
            except Exception:
                log.exception("Delivery failed for destination %s", chat_id)

        seen.add(key)
        save_state(seen)
        time.sleep(1)


if __name__ == "__main__":
    main()
