
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
STATE_VERSION = 3
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


# ==================== FETCH EITAA ====================

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


# ==================== EXTRACT MEDIA ====================

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

    if not any(item["url"] == url for item in items):
        items.append({"url": url, "kind": kind})


def extract_media(container):
    items = []

    # Images
    for img in container.select("img"):
        for attr in (
            "src", "data-src", "data-original",
            "data-lazy-src", "data-url",
        ):
            add_media(items, img.get(attr), "photo")

    # Background images
    for element in container.select("[style]"):
        style = element.get("style", "")

        for value in re.findall(
            r"""url\(\s*['"]?(.*?)['"]?\s*\)""",
            style,
        ):
            add_media(items, value, "auto")

    # Video and video sources
    for element in container.select("video, video source, source"):
        add_media(
            items,
            element.get("src") or element.get("data-src"),
            "video",
        )

        if element.name == "video":
            add_media(items, element.get("poster"), "photo")

    # Direct file links
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

    # Lazy-loaded media attributes
    for element in container.select(
        "[data-src], [data-original], [data-url]"
    ):
        for attr in ("data-src", "data-original", "data-url"):
            add_media(items, element.get(attr), "auto")

    return items


# ==================== EXTRACT MESSAGES ====================

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
            if text_element else ""
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

    # Send in oldest-to-newest order when the page lists newest first.
    messages.reverse()

    logging.info("Parsed %d Eitaa messages", len(messages))

    for message in messages:
        logging.info(
            "Message id=%s text_chars=%d media_count=%d",
            message["id"],
            len(message["text"]),
            len(message["media"]),
        )

    return messages


# ==================== DOWNLOAD MEDIA ====================

def download_media(item):
    try:
        with session.get(
            item["url"],
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
                    logging.warning(
                        "Media exceeds size limit: %s",
                        item["url"],
                    )
                    return None

                chunks.append(chunk)

            content = b"".join(chunks)

            if not content:
                return None

            path = urlparse(response.url).path
            filename = unquote(path.rsplit("/", 1)[-1]) or "media"

        if "." not in filename:
            extension = mimetypes.guess_extension(
                content_type or "application/octet-stream"
            )
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
        logging.warning("Media download failed: %s", exc)
        return None


# ==================== TELEGRAM API ====================

def telegram_request(method, data=None, files=None):
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
    for start in range(0, len(text), 4000):
        part = text[start:start + 4000]

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
                "Could not download media for message %s",
                message["id"],
            )

    # Do not lose the text if media is inaccessible.
    if not downloaded:
        if text:
            send_text(chat_id, text)
            logging.warning(
                "Text sent without media for message %s",
                message["id"],
            )
            return

        raise RuntimeError(
            f"No downloadable media for message {message['id']}"
        )

    send_media(
        chat_id,
        downloaded[0],
        text[:1000] if text else "",
    )

    if len(text) > 1000:
        send_text(chat_id, text[1000:])

    # Send additional photos/files individually.
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

    # First run: initialize state, but DO NOT skip current posts.
    if not state.get("initialized"):
        state["initialized"] = True
        state["version"] = STATE_VERSION
        state["messages"] = stored
        save_state(state)

        logging.info(
            "First run: current visible posts will now be forwarded."
        )

    # Migrate old state. Previous versions may have incorrectly marked
    # existing posts as already sent, so retry them at both destinations.
    elif state.get("version") != STATE_VERSION:
        for message_id, entry in list(stored.items()):
            if not isinstance(entry, dict):
                entry = {}

            entry["sent_to"] = []
            stored[message_id] = entry

        state["version"] = STATE_VERSION
        save_state(state)

        logging.info(
            "State upgraded: previously recorded posts will be retried."
        )

    errors = 0
    new_ids = 0

    for message in messages:
        message_id = message["id"]
        fingerprint = message["fingerprint"]

        is_new_id = message_id not in stored

        if is_new_id:
            new_ids += 1
            entry = {
                "fingerprint": fingerprint,
                "sent_to": [],
            }
            stored[message_id] = entry
            save_state(state)
        else:
            entry = stored[message_id]

            if not isinstance(entry, dict):
                entry = {
                    "fingerprint": fingerprint,
                    "sent_to": [],
                }

            # Do not resend an edited post that has already been delivered.
            entry.setdefault("sent_to", [])

            if not isinstance(entry["sent_to"], list):
                entry["sent_to"] = []

            entry["fingerprint"] = fingerprint
            stored[message_id] = entry
            save_state(state)

        # Send only to destinations that have not succeeded yet.
        for chat_id in CHAT_IDS:
            if chat_id in entry["sent_to"]:
                continue

            try:
                deliver_message(chat_id, message)

                entry["sent_to"].append(chat_id)
                stored[message_id] = entry
                save_state(state)

                logging.info(
                    "Forwarded Eitaa message %s to %s",
                    message_id,
                    chat_id,
                )

            except Exception as exc:
                errors += 1
                logging.exception(
                    "Failed forwarding message %s to %s: %s",
                    message_id,
                    chat_id,
                    exc,
                )

    state["initialized"] = True
    state["version"] = STATE_VERSION
    state["messages"] = stored
    save_state(state)

    logging.info("New message IDs found: %d", new_ids)

    if errors:
        raise RuntimeError(
            f"{errors} delivery operation(s) failed. Check logs."
        )

    logging.info("Forwarding run completed successfully.")


if __name__ == "__main__":
    main()
