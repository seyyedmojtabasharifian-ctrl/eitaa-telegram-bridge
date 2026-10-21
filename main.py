
import os
import re
import json
import hashlib
import logging
import mimetypes
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

# ==================== CONFIG ====================

EITAA_URL = os.environ.get("EITAA_URL", "").strip()
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

CHAT_IDS = [
    item.strip()
    for item in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",")
    if item.strip()
]

STATE_FILE = Path("data/seen.json")
STATE_VERSION = 2
MAX_MEDIA_SIZE = 45 * 1024 * 1024

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Mobile Safari/537.36"
    )
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

session = requests.Session()
session.headers.update(HEADERS)


# ==================== STATE ====================

def load_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    return {
        "version": STATE_VERSION,
        "initialized": False,
        "messages": {},
    }


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")

    temp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(STATE_FILE)


def save_seen(state, message_id, fingerprint, sent_to):
    state["messages"][message_id] = {
        "fingerprint": fingerprint,
        "sent_to": list(dict.fromkeys(sent_to)),
    }
    save_state(state)


# ==================== EITAA PAGE ====================

def get_page():
    if not EITAA_URL:
        raise RuntimeError("Secret EITAA_URL is missing.")

    response = session.get(EITAA_URL, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    logging.info("Eitaa HTTP status: %s", response.status_code)
    logging.info("HTML length: %s", len(response.text))
    logging.info(
        "Page title: %s",
        soup.title.get_text(" ", strip=True) if soup.title else "unknown",
    )

    return soup


# ==================== MEDIA EXTRACTION ====================

def absolute_url(value):
    if not value:
        return None

    value = value.strip()

    if value.startswith(("data:", "javascript:", "#")):
        return None

    url = urljoin(EITAA_URL, value)

    if urlparse(url).scheme not in ("http", "https"):
        return None

    return url


def add_media(items, value, kind="auto"):
    url = absolute_url(value)

    if not url:
        return

    if any(item["url"] == url for item in items):
        return

    items.append({"url": url, "kind": kind})


def extract_media(container):
    items = []

    # Images and lazy-loaded images
    for img in container.select("img"):
        for attr in (
            "src",
            "data-src",
            "data-original",
            "data-lazy-src",
            "data-url",
        ):
            if img.get(attr):
                add_media(items, img.get(attr), "photo")

    # Images/videos embedded as CSS backgrounds
    for element in container.select("[style]"):
        style = element.get("style", "")

        for value in re.findall(
            r"""url\(\s*['"]?(.*?)['"]?\s*\)""",
            style,
        ):
            add_media(items, value, "auto")

    # Videos and their sources
    for element in container.select("video, video source"):
        add_media(
            items,
            element.get("src") or element.get("data-src"),
            "video",
        )

        if element.name == "video":
            add_media(items, element.get("poster"), "photo")

    # Links to direct files or media-related elements
    extensions = (
        ".jpg", ".jpeg", ".png", ".webp", ".gif",
        ".mp4", ".mov", ".webm", ".m4v",
        ".pdf", ".zip", ".rar", ".7z",
        ".doc", ".docx", ".xls", ".xlsx",
        ".ppt", ".pptx", ".txt", ".apk",
        ".mp3", ".m4a", ".ogg", ".wav",
    )

    for link in container.select("a[href]"):
        href = link.get("href", "")
        path = urlparse(href).path.lower()
        classes = " ".join(link.get("class", [])).lower()

        if any(path.endswith(ext) for ext in extensions):
            add_media(items, href, "auto")
        elif any(word in classes for word in (
            "document", "file", "video", "audio", "photo",
        )):
            add_media(items, href, "auto")

    # Additional media attributes used by some page templates
    for element in container.select(
        "[data-src], [data-original], [data-url]"
    ):
        for attr in ("data-src", "data-original", "data-url"):
            if element.get(attr):
                add_media(items, element.get(attr), "auto")

    return items


# ==================== MESSAGE EXTRACTION ====================

def extract_messages(soup):
    containers = soup.select(
        "div.etme_widget_message_wrap, "
        "div.js-widget_message_wrap"
    )

    messages = []
    used_ids = set()

    for container in containers:
        message_id = str(container.get("id", "")).strip()

        if not message_id or message_id in used_ids:
            continue

        used_ids.add(message_id)

        text_element = container.select_one(
            ".js-message_text, .etme_widget_message_text"
        )

        text = (
            text_element.get_text("\n", strip=True)
            if text_element
            else ""
        )

        media = extract_media(container)

        if not text and not media:
            continue

        raw = json.dumps(
            {
                "id": message_id,
                "text": text,
                "media": [item["url"] for item in media],
            },
            ensure_ascii=False,
            sort_keys=True,
        )

        fingerprint = hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()

        messages.append({
            "id": message_id,
            "text": text,
            "media": media,
            "fingerprint": fingerprint,
        })

    # The public page commonly lists newest posts first.
    messages.reverse()

    logging.info("Parsed %d Eitaa messages", len(messages))

    for message in messages:
        logging.info(
            "Found message id=%s, text_chars=%d, media_count=%d",
            message["id"],
            len(message["text"]),
            len(message["media"]),
        )

    return messages


# ==================== DOWNLOAD MEDIA ====================

def download_media(item):
    url = item["url"]

    try:
        with session.get(
            url,
            timeout=40,
            stream=True,
        ) as response:
            response.raise_for_status()

            content_type = response.headers.get(
                "Content-Type", ""
            ).split(";")[0].strip().lower()

            chunks = []
            total = 0

            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue

                total += len(chunk)

                if total > MAX_MEDIA_SIZE:
                    logging.warning("Media exceeds size limit: %s", url)
                    return None

                chunks.append(chunk)

            content = b"".join(chunks)

            if not content:
                logging.warning("Empty media response: %s", url)
                return None

            path = urlparse(response.url).path
            filename = unquote(path.rsplit("/", 1)[-1]) or "media"

        if "." not in filename:
            extension = mimetypes.guess_extension(content_type)
            if extension:
                filename += extension

        kind = item["kind"]

        if kind == "auto":
            if content_type.startswith("image/"):
                kind = "photo"
            elif content_type.startswith("video/"):
                kind = "video"
            elif content_type.startswith("audio/"):
                kind = "audio"
            else:
                kind = "document"

        return {
            "content": content,
            "filename": filename,
            "content_type": content_type or "application/octet-stream",
            "kind": kind,
        }

    except requests.RequestException as exc:
        logging.warning("Could not download media: %s", exc)
        return None


# ==================== TELEGRAM API ====================

def telegram_request(method, data=None, files=None):
    if not BOT_TOKEN:
        raise RuntimeError("Secret TELEGRAM_BOT_TOKEN is missing.")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

    response = requests.post(
        url,
        data=data,
        files=files,
        timeout=60,
    )

    try:
        result = response.json()
    except ValueError:
        result = {}

    if not response.ok or not result.get("ok"):
        description = result.get("description", response.text[:500])
        raise RuntimeError(f"Telegram {method}: {description}")

    return result


def send_text(chat_id, text):
    # Telegram text limit is 4096 characters.
    parts = [
        text[i:i + 4000]
        for i in range(0, len(text), 4000)
    ]

    for part in parts:
        telegram_request(
            "sendMessage",
            data={
                "chat_id": chat_id,
                "text": part,
                "disable_web_page_preview": "true",
            },
        )


def send_media(chat_id, media, caption=""):
    methods = {
        "photo": ("sendPhoto", "photo"),
        "video": ("sendVideo", "video"),
        "audio": ("sendAudio", "audio"),
        "document": ("sendDocument", "document"),
    }

    method, field = methods.get(
        media["kind"],
        ("sendDocument", "document"),
    )

    data = {"chat_id": chat_id}

    if caption:
        data["caption"] = caption[:1000]

    files = {
        field: (
            media["filename"],
            media["content"],
            media["content_type"],
        )
    }

    telegram_request(method, data=data, files=files)


def deliver_message(chat_id, message):
    text = message["text"].strip()
    media_items = message["media"]

    if not media_items:
        if text:
            send_text(chat_id, text)
        return

    downloaded = []

    for item in media_items:
        media = download_media(item)

        if media:
            downloaded.append(media)
        else:
            logging.warning(
                "Media unavailable for message %s",
                message["id"],
            )

    # If the media is inaccessible, at least preserve the text.
    if not downloaded:
        if text:
            send_text(chat_id, text)
            logging.warning(
                "Sent text only; media could not be downloaded "
                "for message %s",
                message["id"],
            )
            return

        raise RuntimeError(
            f"Could not download any media for message {message['id']}"
        )

    caption = text[:1000] if text else ""
    send_media(chat_id, downloaded[0], caption)

    if len(text) > 1000:
        send_text(chat_id, text[1000:])

    # Send remaining attachments individually.
    for media in downloaded[1:]:
        send_media(chat_id, media)


# ==================== MAIN ====================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Secret TELEGRAM_BOT_TOKEN is missing.")

    if not CHAT_IDS:
        raise RuntimeError("Secret TELEGRAM_CHAT_IDS is missing.")

    soup = get_page()
    messages = extract_messages(soup)

    if not messages:
        raise RuntimeError(
            "No messages found. Check the Eitaa HTML and logs."
        )

    state = load_state()
    stored = state.get("messages", {})

    if not isinstance(stored, dict):
        stored = {}

    state["messages"] = stored

    # FIRST RUN: save the current posts as baseline.
    # They will NOT be forwarded.
    if not state.get("initialized"):
        for message in messages:
            stored[message["id"]] = {
                "fingerprint": message["fingerprint"],
                "sent_to": CHAT_IDS.copy(),
            }

        state["initialized"] = True
        state["version"] = STATE_VERSION
        save_state(state)

        logging.info(
            "Baseline saved: %d existing posts will not be sent. "
            "Only posts first appearing in a later run are forwarded.",
            len(messages),
        )
        return

    # Migrate the previous version's state without resending old posts.
    if state.get("version") != STATE_VERSION:
        for message_id, entry in list(stored.items()):
            if not isinstance(entry, dict):
                entry = {}

            entry["sent_to"] = CHAT_IDS.copy()
            stored[message_id] = entry

        state["version"] = STATE_VERSION
        state["messages"] = stored
        save_state(state)

        logging.info(
            "Migrated old state. Previously known posts will not be sent."
        )

    errors = 0
    new_count = 0

    for message in messages:
        message_id = message["id"]
        fingerprint = message["fingerprint"]

        # Existing message IDs are never forwarded again, even if edited.
        if message_id in stored:
            entry = stored[message_id]

            if not isinstance(entry, dict):
                entry = {}

            if entry.get("fingerprint") != fingerprint:
                entry["fingerprint"] = fingerprint
                entry["sent_to"] = CHAT_IDS.copy()
                stored[message_id] = entry
                save_state(state)

            continue

        # An unseen ID means a post not present in our stored history.
        new_count += 1
        entry = {
            "fingerprint": fingerprint,
            "sent_to": [],
        }
        stored[message_id] = entry
        save_state(state)

        for chat_id in CHAT_IDS:
            if chat_id in entry["sent_to"]:
                continue

            try:
                deliver_message(chat_id, message)

                entry["sent_to"].append(chat_id)
                stored[message_id] = entry
                save_state(state)

                logging.info(
                    "Forwarded new message %s to destination %s",
                    message_id,
                    chat_id,
                )

            except Exception as exc:
                errors += 1
                logging.exception(
                    "Failed message %s to destination %s: %s",
                    message_id,
                    chat_id,
                    exc,
                )

    save_state(state)

    logging.info("New message IDs found: %d", new_count)

    if errors:
        raise RuntimeError(
            f"{errors} delivery operation(s) failed. See logs above."
        )

    logging.info("Run completed successfully.")


if __name__ == "__main__":
    main()
