import os
import re
import json
import hashlib
import logging
import mimetypes
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote
from email.message import Message

import requests
from bs4 import BeautifulSoup

# ==================== CONFIG ====================
EITAA_URL = os.environ.get("EITAA_URL", "").strip()
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [
    value.strip()
    for value in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",")
    if value.strip()
]

STATE_FILE = Path("data/seen.json")
STATE_VERSION = 4
MAX_MEDIA_SIZE = 45 * 1024 * 1024
TIMEOUT = (15, 60)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36"
    )
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
session = requests.Session()
session.headers.update(HEADERS)
MEDIA_BASE_URL = ""


# ==================== STATE ====================
def load_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            value.setdefault("messages", {})
            return value
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": STATE_VERSION, "initialized": False, "messages": {}}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(STATE_FILE)


# ==================== FETCH EITAA ====================
def fetch_html(url):
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "").lower()
    if "text/html" not in content_type and not response.text.lstrip().startswith("<"):
        raise RuntimeError(f"URL did not return HTML: {url} ({content_type})")
    return response.text, response.url


def get_page():
    global MEDIA_BASE_URL
    if not EITAA_URL:
        raise RuntimeError("Secret EITAA_URL is missing.")

    html, final_url = fetch_html(EITAA_URL)
    MEDIA_BASE_URL = final_url
    soup = BeautifulSoup(html, "html.parser")

    # A public Eitaa post page often wraps the real post in an iframe.
    # If the outer page has no messages, load that iframe page.
    selectors = "div.etme_widget_message_wrap, div.js-widget_message_wrap"
    if not soup.select(selectors):
        iframe = soup.select_one("iframe[src]")
        if iframe:
            iframe_url = urljoin(final_url, iframe.get("src", ""))
            if urlparse(iframe_url).netloc.endswith("eitaa.com"):
                logging.info("Outer page has no messages; loading embedded page.")
                embedded_html, embedded_final_url = fetch_html(iframe_url)
                embedded = BeautifulSoup(embedded_html, "html.parser")
                if embedded.select(selectors):
                    soup = embedded
                    MEDIA_BASE_URL = embedded_final_url

    logging.info("Eitaa page title: %s", soup.title.get_text(" ", strip=True) if soup.title else "unknown")
    logging.info("HTML length: %d", len(str(soup)))
    return soup


def absolute_url(value):
    if not value:
        return None
    value = value.strip()
    if value.startswith(("data:", "javascript:", "#")):
        return None
    result = urljoin(MEDIA_BASE_URL or EITAA_URL, value)
    if urlparse(result).scheme not in ("http", "https"):
        return None
    return result


def add_media(items, value, kind="auto"):
    url = absolute_url(value)
    if not url:
        return
    # Ignore fragments and deduplicate URL variants with the same media path.
    parsed = urlparse(url)
    key = (parsed.netloc.lower(), unquote(parsed.path).rstrip("/"))
    for existing in items:
        old = urlparse(existing["url"])
        old_key = (old.netloc.lower(), unquote(old.path).rstrip("/"))
        if key == old_key:
            # Keep the more specific media type if a URL was discovered twice.
            if existing["kind"] == "auto" and kind != "auto":
                existing["kind"] = kind
            return
    items.append({"url": url, "kind": kind})


def extract_media(container):
    items = []

    # Real image sources; do not collect site logos outside the message container.
    for img in container.select("img"):
        for attr in ("data-original", "data-src", "data-lazy-src", "src", "data-url"):
            add_media(items, img.get(attr), "photo")

    for video in container.select("video"):
        add_media(items, video.get("src") or video.get("data-src"), "video")
        for source in video.select("source[src], source[data-src]"):
            add_media(items, source.get("src") or source.get("data-src"), "video")
        # Poster is a preview image, not the actual video.
        # Do not forward poster as a separate media item.

    for source in container.select("video source[src], video source[data-src]"):
        add_media(items, source.get("src") or source.get("data-src"), "video")

    # Eitaa pages may expose media through data attributes or a download link.
    extensions = (
        ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp",
        ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".3gp",
        ".pdf", ".zip", ".rar", ".7z", ".doc", ".docx", ".xls",
        ".xlsx", ".ppt", ".pptx", ".txt", ".apk", ".mp3", ".m4a",
        ".ogg", ".wav", ".flac", ".aac",
    )
    for link in container.select("a[href]"):
        href = link.get("href", "")
        parsed_path = unquote(urlparse(href).path).lower()
        classes = " ".join(link.get("class", [])).lower()
        label = link.get_text(" ", strip=True).lower()
        if parsed_path.endswith(extensions):
            ext = Path(parsed_path).suffix.lower()
            kind = "video" if ext in {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".3gp"} else "auto"
            add_media(items, href, kind)
        elif any(word in classes for word in ("document", "file", "video", "audio", "photo", "media")):
            kind = "video" if "video" in classes else "auto"
            add_media(items, href, kind)
        elif any(word in label for word in ("دانلود فایل", "دریافت فایل", "download file")):
            add_media(items, href, "auto")

    for element in container.select("[data-src], [data-original], [data-url], [data-file-url]"):
        for attr in ("data-file-url", "data-original", "data-src", "data-url"):
            value = element.get(attr)
            if value:
                add_media(items, value, "auto")
    return items


def message_number(message_id):
    match = re.search(r"(\d+)(?!.*\d)", message_id)
    return int(match.group(1)) if match else -1


def extract_messages(soup):
    containers = soup.select("div.etme_widget_message_wrap, div.js-widget_message_wrap")
    messages, used_ids = [], set()

    for container in containers:
        message_id = str(container.get("id", "")).strip()
        if not message_id or message_id in used_ids:
            continue
        used_ids.add(message_id)

        text_element = container.select_one(".js-message_text, .etme_widget_message_text")
        text = text_element.get_text("\n", strip=True) if text_element else ""
        media = extract_media(container)
        if not text and not media:
            continue

        raw = json.dumps(
            {"id": message_id, "text": text, "media": media},
            ensure_ascii=False, sort_keys=True,
        )
        fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        messages.append({
            "id": message_id,
            "text": text,
            "media": media,
            "fingerprint": fingerprint,
        })

    # Sort by the numeric Eitaa post ID, oldest first. Do not blindly reverse HTML.
    messages.sort(key=lambda item: (message_number(item["id"]), item["id"]))
    logging.info("Parsed %d Eitaa messages", len(messages))
    for message in messages:
        logging.info(
            "Message id=%s text_chars=%d media_count=%d",
            message["id"], len(message["text"]), len(message["media"]),
        )
    return messages


# ==================== MEDIA DOWNLOAD / DETECTION ====================
def filename_from_disposition(header):
    if not header:
        return None
    message = Message()
    message["content-disposition"] = header
    try:
        name = message.get_filename()
        return unquote(name).replace("\\", "/").rsplit("/", 1)[-1] if name else None
    except Exception:
        return None


def sniff_type(content):
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if len(content) > 12 and content[4:8] == b"ftyp":
        # MP4/M4V/MOV family. Telegram sendVideo is best with .mp4.
        brand = content[8:12].lower()
        if brand in (b"qt  ",):
            return "video/quicktime", ".mov"
        if brand in (b"m4v ",):
            return "video/x-m4v", ".m4v"
        return "video/mp4", ".mp4"
    if content.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm", ".webm"
    if content.startswith(b"%PDF-"):
        return "application/pdf", ".pdf"
    if content.startswith(b"PK\x03\x04"):
        return "application/zip", ".zip"
    if content.startswith(b"ID3") or content.startswith(b"\xff\xfb"):
        return "audio/mpeg", ".mp3"
    if content.startswith(b"OggS"):
        return "audio/ogg", ".ogg"
    return None, None


def download_media(item):
    try:
        with session.get(item["url"], timeout=TIMEOUT, stream=True) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            disposition_name = filename_from_disposition(response.headers.get("Content-Disposition"))
            chunks, total = [], 0
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_MEDIA_SIZE:
                    logging.warning("Media exceeds %d bytes: %s", MAX_MEDIA_SIZE, item["url"])
                    return None
                chunks.append(chunk)
            content = b"".join(chunks)
            final_url = response.url

        if not content:
            return None

        # Prevent an HTML error/login page from being sent as a .html/.bin attachment.
        sniffed_type, sniffed_ext = sniff_type(content[:64])
        if content_type in ("text/html", "application/xhtml+xml") or content.lstrip()[:20].lower().startswith((b"<!doctype html", b"<html")):
            logging.warning("Media URL returned HTML instead of a file: %s", final_url)
            return None

        parsed_name = unquote(urlparse(final_url).path.rsplit("/", 1)[-1])
        filename = disposition_name or parsed_name or "media"
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        ext = Path(filename).suffix.lower()

        if sniffed_type:
            # File signatures are more reliable than URL extensions/content-type.
            content_type = sniffed_type
            if not ext or ext in {".bin", ".html", ".htm", ".php", ".asp", ".aspx"}:
                filename = (Path(filename).stem if ext else filename) + sniffed_ext
                ext = sniffed_ext
        elif not ext or ext in {".bin", ".html", ".htm", ".php", ".asp", ".aspx"}:
            guessed = mimetypes.guess_extension(content_type or "")
            if guessed and guessed not in (".bin", ".html", ".htm"):
                filename = Path(filename).stem + guessed
                ext = guessed

        # Generic or misleading MIME type: infer from the final URL extension.
        if not content_type or content_type == "application/octet-stream":
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

        kind = item["kind"]
        if kind == "auto":
            if content_type.startswith("image/"):
                kind = "photo"
            elif content_type.startswith("video/") or ext in {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".3gp"}:
                kind = "video"
            elif content_type.startswith("audio/"):
                kind = "audio"
            else:
                kind = "document"

        # Telegram's sendVideo is most reliable for MP4. Keep other video formats as files.
        if kind == "video" and ext != ".mp4":
            kind = "document"

        logging.info("Downloaded media name=%s type=%s kind=%s bytes=%d", filename, content_type, kind, len(content))
        return {
            "content": content,
            "filename": filename or "media",
            "content_type": content_type,
            "kind": kind,
        }
    except requests.RequestException as exc:
        logging.warning("Media download failed (%s): %s", item["url"], exc)
        return None


# ==================== TELEGRAM API ====================
def telegram_request(method, data=None, files=None):
    if not BOT_TOKEN:
        raise RuntimeError("Secret TELEGRAM_BOT_TOKEN is missing.")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    response = requests.post(url, data=data, files=files, timeout=90)
    try:
        result = response.json()
    except ValueError:
        result = {}
    if not response.ok or not result.get("ok"):
        description = result.get("description", response.text[:500])
        raise RuntimeError(f"Telegram {method}: {description}")
    return result


def send_text(chat_id, text):
    if not text:
        return
    # Telegram message limit is 4096 characters; leave margin.
    for start in range(0, len(text), 4000):
        telegram_request("sendMessage", data={
            "chat_id": chat_id,
            "text": text[start:start + 4000],
            "disable_web_page_preview": "true",
        })


def send_media(chat_id, media, caption=""):
    method_field = {
        "photo": ("sendPhoto", "photo"),
        "video": ("sendVideo", "video"),
        "audio": ("sendAudio", "audio"),
        "document": ("sendDocument", "document"),
    }
    method, field = method_field.get(media["kind"], ("sendDocument", "document"))
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:1000]
    files = {field: (media["filename"], media["content"], media["content_type"])}
    telegram_request(method, data=data, files=files)


def deliver_message(chat_id, message):
    text = message["text"].strip()
    downloaded = []
    for item in message["media"]:
        media = download_media(item)
        if media:
            downloaded.append(media)
        else:
            logging.warning("Skipping inaccessible media in message %s", message["id"])

    if not downloaded:
        if text:
            send_text(chat_id, text)
            return
        if message["media"]:
            raise RuntimeError(f"All media downloads failed for {message['id']}")
        return

    # Send first media with caption, then remaining media in their original order.
    send_media(chat_id, downloaded[0], text[:1000] if text else "")
    if len(text) > 1000:
        send_text(chat_id, text[1000:])
    for media in downloaded[1:]:
        send_media(chat_id, media)


# ==================== MAIN ====================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("Secret TELEGRAM_BOT_TOKEN is missing.")
    if not CHAT_IDS:
        raise RuntimeError("Secret TELEGRAM_CHAT_IDS is missing.")

    messages = extract_messages(get_page())
    if not messages:
        raise RuntimeError(
            "No Eitaa messages found. EITAA_URL must point to a public channel/post page "
            "whose embedded HTML contains message elements. Check the workflow logs."
        )

    state = load_state()
    stored = state.get("messages", {})
    if not isinstance(stored, dict):
        stored = {}
    state["messages"] = stored

    # Optional one-time recovery: re-attempt visible posts that were marked sent
    # by an earlier run, even if they never actually appeared in Telegram.
    # Set GitHub Secret/Variable RESET_SENT_HISTORY_ONCE=true for one run.
    # The state marker prevents repeated resends on subsequent workflow runs.
    reset_requested = os.environ.get("RESET_SENT_HISTORY_ONCE", "").strip().lower() in {
        "1", "true", "yes", "on"
    }
    if reset_requested and not state.get("sent_history_reset_done"):
        reset_count = 0
        for saved_entry in stored.values():
            if isinstance(saved_entry, dict):
                saved_entry["sent_to"] = []
                reset_count += 1
        state["sent_history_reset_done"] = True
        state["messages"] = stored
        save_state(state)
        logging.warning(
            "One-time recovery reset cleared sent_to for %d stored messages.",
            reset_count,
        )

    if not state.get("initialized"):
        state["initialized"] = True
        state["version"] = STATE_VERSION
        save_state(state)
        logging.info("First run: forwarding currently visible posts.")
    elif state.get("version") != STATE_VERSION:
        # Preserve successful destination history to avoid resending everything
        # merely because code was updated.
        state["version"] = STATE_VERSION
        save_state(state)
        logging.info("State schema upgraded; successful destinations preserved.")

    errors = 0
    for message in messages:  # sorted oldest-to-newest above
        message_id = message["id"]
        entry = stored.get(message_id)
        if not isinstance(entry, dict):
            entry = {"fingerprint": message["fingerprint"], "sent_to": []}
        if not isinstance(entry.get("sent_to"), list):
            entry["sent_to"] = []

        # If a message changes, do not resend its entire history automatically.
        entry["fingerprint"] = message["fingerprint"]
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
                logging.info("Forwarded message %s to destination %s", message_id, chat_id)
            except Exception:
                errors += 1
                logging.exception("Failed forwarding message %s to %s", message_id, chat_id)

    state["initialized"] = True
    state["version"] = STATE_VERSION
    state["messages"] = stored
    save_state(state)

    if errors:
        raise RuntimeError(f"{errors} delivery operation(s) failed. Check GitHub Actions logs.")
    logging.info("Forwarding run completed successfully.")


if __name__ == "__main__":
    main()
