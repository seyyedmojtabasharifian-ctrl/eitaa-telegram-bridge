
import os
import re
import json
import time
import hashlib
import logging
import mimetypes
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

# -------------------- تنظیمات --------------------

EITAA_URL = os.environ.get("EITAA_URL", "").strip()
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

CHAT_IDS = [
    x.strip()
    for x in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",")
    if x.strip()
]

STATE_FILE = Path("data/seen.json")
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
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()
session.headers.update(HEADERS)


# -------------------- دریافت صفحه --------------------

def get_page():
    if not EITAA_URL:
        raise RuntimeError("Secret EITAA_URL تنظیم نشده است.")

    response = session.get(EITAA_URL, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    logging.info("Eitaa HTTP status: %s", response.status_code)
    logging.info("HTML length: %s", len(response.text))
    logging.info("Page title: %s", soup.title.get_text(" ", strip=True)
                 if soup.title else "بدون عنوان")

    return soup


# -------------------- مدیریت وضعیت --------------------

def load_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = STATE_FILE.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    temp_file.replace(STATE_FILE)


# -------------------- استخراج رسانه --------------------

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


def add_media(items, url, kind="auto"):
    url = absolute_url(url)
    if not url:
        return

    # حذف موارد تکراری
    if any(item["url"] == url for item in items):
        return

    items.append({"url": url, "kind": kind})


def extract_media(container):
    items = []

    # عکس‌های داخل پیام
    photo_selectors = [
        ".etme_widget_message_photo_wrap",
        ".etme_widget_message_photo",
        ".js-message_photo",
        "a[class*='photo_wrap']",
        "a[class*='message_photo']",
    ]

    for selector in photo_selectors:
        for element in container.select(selector):
            style = element.get("style", "")
            for match in re.findall(
                r"""url\(['"]?(.*?)['"]?\)""", style
            ):
                add_media(items, match, "photo")

            for img in element.select("img"):
                for attr in ("src", "data-src", "data-original"):
                    add_media(items, img.get(attr), "photo")

            if element.name == "a":
                add_media(items, element.get("href"), "photo")

    # ویدیو و منبع ویدیو
    for element in container.select("video, video source, source"):
        add_media(
            items,
            element.get("src") or element.get("data-src"),
            "video"
        )
        if element.name == "video":
            add_media(items, element.get("poster"), "photo")

    # فایل‌ها و لینک‌های مستقیم رسانه
    extensions = (
        ".jpg", ".jpeg", ".png", ".webp", ".gif",
        ".mp4", ".mov", ".webm", ".m4v",
        ".pdf", ".zip", ".rar", ".7z",
        ".doc", ".docx", ".xls", ".xlsx",
        ".ppt", ".pptx", ".txt", ".apk",
        ".mp3", ".m4a", ".ogg", ".wav"
    )

    for link in container.select("a[href]"):
        href = link.get("href", "")
        path = urlparse(href).path.lower()

        if any(path.endswith(ext) for ext in extensions):
            add_media(items, href, "auto")

    # تصاویر دارای آدرس در data attributes
    for element in container.select(
        "[data-src], [data-original], [data-url]"
    ):
        for attr in ("data-src", "data-original", "data-url"):
            value = element.get(attr)
            if value:
                add_media(items, value, "auto")

    return items


# -------------------- استخراج پیام‌های ایتا --------------------

def extract_messages(soup):
    messages = []

    # کلاس واقعی پیام‌ها از گزارش HTML ایتا شناسایی شده است.
    containers = soup.select(
        "div.etme_widget_message_wrap, "
        "div.js-widget_message_wrap"
    )

    # جلوگیری از انتخاب تکراری یک عنصر
    unique = {}
    for container in containers:
        message_id = str(container.get("id", "")).strip()

        if message_id and message_id not in unique:
            unique[message_id] = container

    for message_id, container in unique.items():
        text_element = container.select_one(
            ".js-message_text, .etme_widget_message_text"
        )

        text = (
            text_element.get_text("\n", strip=True)
            if text_element else ""
        )

        media = extract_media(container)

        # پیام‌های بدون متن و بدون رسانه نادیده گرفته می‌شوند.
        if not text and not media:
            continue

        fingerprint_source = json.dumps(
            {
                "id": message_id,
                "text": text,
                "media": [m["url"] for m in media],
            },
            ensure_ascii=False,
            sort_keys=True,
        )

        fingerprint = hashlib.sha256(
            fingerprint_source.encode("utf-8")
        ).hexdigest()

        messages.append({
            "id": message_id,
            "text": text,
            "media": media,
            "fingerprint": fingerprint,
        })

    # ترتیب نمایش صفحه معمولاً از جدید به قدیم است.
    # ارسال را از قدیمی به جدید انجام می‌دهیم.
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


# -------------------- دریافت فایل رسانه --------------------

def download_media(item):
    url = item["url"]

    try:
        response = session.get(
            url,
            timeout=40,
            stream=True,
        )
        response.raise_for_status()

        content_type = response.headers.get(
            "Content-Type", ""
        ).split(";")[0].strip().lower()

        chunks = []
        total = 0

        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue

            total += len(chunk)

            if total > MAX_MEDIA_SIZE:
                logging.warning("Media too large; skipped: %s", url)
                response.close()
                return None

            chunks.append(chunk)

        content = b"".join(chunks)

        if not content:
            return None

        path = urlparse(response.url).path
        filename = unquote(path.rsplit("/", 1)[-1]) or "media"

        if "." not in filename:
            extension = mimetypes.guess_extension(
                content_type or ""
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


# -------------------- ارسال به تلگرام --------------------

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
        raise RuntimeError(
            f"Telegram {method} failed: {description}"
        )

    return result


def send_text(chat_id, text):
    # تقسیم متن‌های طولانی به قطعات قابل ارسال
    parts = [text[i:i + 4000] for i in range(0, len(text), 4000)]

    for part in parts:
        telegram_request(
            "sendMessage",
            data={
                "chat_id": chat_id,
                "text": part,
                "disable_web_page_preview": "true",
            },
        )


def send_one_media(chat_id, media, caption=""):
    kind = media["kind"]

    method_by_kind = {
        "photo": "sendPhoto",
        "video": "sendVideo",
        "audio": "sendAudio",
        "document": "sendDocument",
    }

    method = method_by_kind.get(kind, "sendDocument")

    field_by_method = {
        "sendPhoto": "photo",
        "sendVideo": "video",
        "sendAudio": "audio",
        "sendDocument": "document",
    }

    field = field_by_method[method]

    data = {"chat_id": chat_id}

    if caption:
        # محدودیت کپشن تلگرام
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
                "A media item could not be downloaded for message %s",
                message["id"],
            )

    if not downloaded:
        raise RuntimeError(
            f"No media could be downloaded for message {message['id']}"
        )

    # کپشن را روی رسانه اول قرار می‌دهیم.
    caption = text[:1000] if text else ""
    send_one_media(chat_id, downloaded[0], caption)

    # اگر متن از حد مجاز کپشن بلندتر باشد، باقی آن هم ارسال می‌شود.
    if len(text) > 1000:
        send_text(chat_id, text[1000:])

    # رسانه‌های اضافی همان پیام
    for media in downloaded[1:]:
        send_one_media(chat_id, media)


# -------------------- اجرای اصلی --------------------

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Secret TELEGRAM_BOT_TOKEN تنظیم نشده است."
        )

    if not CHAT_IDS:
        raise RuntimeError(
            "Secret TELEGRAM_CHAT_IDS تنظیم نشده است."
        )

    soup = get_page()
    messages = extract_messages(soup)

    if not messages:
        raise RuntimeError(
            "هیچ پیام قابل استفاده‌ای پیدا نشد. "
            "لاگ Parsed و ساختار HTML را بررسی کنید."
        )

    state = load_state()
    known_ids = state.get("messages", {})

    if not isinstance(known_ids, dict):
        known_ids = {}

    # اجرای اول: پیام‌های موجود را فقط ثبت می‌کنیم تا
    # همه پیام‌های قدیمی ناخواسته یک‌باره ارسال نشوند.
    if not state.get("initialized"):
        for message in messages:
            known_ids[message["id"]] = {
                "fingerprint": message["fingerprint"],
                "sent_to": [],
            }

        save_state({
            "initialized": True,
            "messages": known_ids,
        })

        logging.info(
            "First run: recorded %d existing messages. "
            "New messages will be forwarded on later runs.",
            len(messages),
        )
        return

    errors = 0

    for message in messages:
        message_id = message["id"]
        entry = known_ids.get(message_id, {})

        if not isinstance(entry, dict):
            entry = {}

        if entry.get("fingerprint") != message["fingerprint"]:
            entry = {
                "fingerprint": message["fingerprint"],
                "sent_to": [],
            }

        sent_to = entry.get("sent_to", [])
        if not isinstance(sent_to, list):
            sent_to = []

        for chat_id in CHAT_IDS:
            if chat_id in sent_to:
                continue

            try:
                deliver_message(chat_id, message)
                sent_to.append(chat_id)
                entry["sent_to"] = sent_to
                known_ids[message_id] = entry

                # ذخیره پس از هر مقصد، برای کاهش احتمال تکرار
                save_state({
                    "initialized": True,
                    "messages": known_ids,
                })

                logging.info(
                    "Forwarded message %s to destination %s",
                    message_id,
                    chat_id,
                )

            except Exception as exc:
                errors += 1
                logging.exception(
                    "Failed forwarding message %s to destination %s: %s",
                    message_id,
                    chat_id,
                    exc,
                )

        known_ids[message_id] = entry

    save_state({
        "initialized": True,
        "messages": known_ids,
    })

    if errors:
        raise RuntimeError(
            f"{errors} forwarding operation(s) failed. "
            "Check the logs above."
        )

    logging.info("Forwarding run completed successfully.")


if __name__ == "__main__":
    main()
