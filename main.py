import os
import re
import json
import time
import hashlib
import logging
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("eitaa-bridge")

SOURCE_URL = os.getenv("EITAA_URL", "https://eitaa.com/oliayazdahoom").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
STATE_PATH = Path("data/seen.json")
MAX_MESSAGES = int(os.getenv("MAX_MESSAGES", "30"))
TIMEOUT = 25

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
})

def clean_text(value):
    return re.sub(r"\n{3,}", "\n\n", (value or "").replace("\xa0", " ")).strip()

def get_page():
    response = SESSION.get(SOURCE_URL, timeout=TIMEOUT)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    return response.text

def candidate_message_nodes(soup):
    """
    Eitaa's public page has no stable documented API. Try common message-card
    patterns and then a conservative structural fallback. Log counts so the
    selectors can be updated if Eitaa changes its HTML.
    """
    found = []
    seen = set()
    selectors = [
        "[data-post-id]", "[data-message-id]", ".post", ".message",
        ".channel-post", ".post-item", ".feed-item", "article"
    ]
    for selector in selectors:
        for node in soup.select(selector):
            if id(node) not in seen:
                seen.add(id(node))
                found.append(node)

    # Fallback: find elements containing links to individual post anchors/times.
    if not found:
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if re.search(r"(?:/oliayazdahoom/|[?&](?:before|post|id)=)", href):
                parent = a
                for _ in range(5):
                    if not isinstance(parent.parent, Tag):
                        break
                    parent = parent.parent
                    txt = clean_text(parent.get_text("\n", strip=True))
                    if len(txt) >= 8 and len(txt) <= 12000:
                        if id(parent) not in seen:
                            seen.add(id(parent))
                            found.append(parent)
                        break
    return found

def extract_media(node, page_url):
    media = []
    # Direct images and videos
    for tag in node.find_all(["img", "video", "source"]):
        src = tag.get("src") or tag.get("data-src") or tag.get("data-original")
        if src:
            full = urljoin(page_url, src)
            if full.startswith(("http://", "https://")):
                media.append((full, "photo" if tag.name == "img" else "video"))
        if tag.name == "video" and tag.get("poster"):
            full = urljoin(page_url, tag["poster"])
            media.append((full, "photo"))

    # Download links or file attachments
    for a in node.find_all("a", href=True):
        href = urljoin(page_url, a["href"])
        label = clean_text(a.get_text(" ", strip=True)).lower()
        path = urlparse(href).path.lower()
        if any(ext in path for ext in (
            ".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov",
            ".m4v", ".pdf", ".zip", ".rar", ".doc", ".docx", ".xls",
            ".xlsx", ".ppt", ".pptx", ".mp3", ".ogg", ".apk"
        )):
            kind = "photo" if any(path.endswith(x) for x in (".jpg", ".jpeg", ".png", ".webp", ".gif")) else \
                   "video" if any(path.endswith(x) for x in (".mp4", ".mov", ".m4v")) else "document"
            media.append((href, kind))
        elif any(word in label for word in ("دانلود", "فایل", "video", "download")):
            media.append((href, "document"))

    # Deduplicate URLs while preserving order
    out, urls = [], set()
    for item in media:
        if item[0] not in urls:
            urls.add(item[0])
            out.append(item)
    return out[:10]

def extract_messages(html):
    soup = BeautifulSoup(html, "html.parser")
    nodes = candidate_message_nodes(soup)
    log.info("HTML parse: found %d candidate message containers", len(nodes))
    messages = []
    seen_fingerprints = set()

    for node in nodes:
        text = clean_text(node.get_text("\n", strip=True))
        # Strip generic page chrome and discard tiny/non-message blocks
        if len(text) < 2 or len(text) > 15000:
            continue
        # Avoid containers that are just the channel title or navigation
        if text in {"مطالب قبلی", "مشاهده در ایتا", "پیوستن"}:
            continue

        post_id = (
            node.get("data-post-id") or node.get("data-message-id") or
            node.get("id") or ""
        )
        if not post_id:
            for a in node.find_all("a", href=True):
                href = a["href"]
                match = re.search(r"(?:/oliayazdahoom/|[?&](?:post|id)=)(\d+)", href)
                if match:
                    post_id = match.group(1)
                    break
        media = extract_media(node, SOURCE_URL)
        # If no stable ID is available, fingerprint text plus media URLs.
        fingerprint = hashlib.sha256(
            (post_id + "\n" + text + "\n" + "\n".join(u for u, _ in media)).encode("utf-8")
        ).hexdigest()
        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)
        messages.append({
            "id": str(post_id or fingerprint),
            "text": text,
            "media": media,
            "fingerprint": fingerprint,
        })

    # Eitaa pages commonly show newest first. Reverse so sends are oldest-first.
    messages = messages[:MAX_MESSAGES]
    messages.reverse()
    return messages

def load_state():
    if not STATE_PATH.exists():
        return None
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return set(data.get("seen", []))
    except Exception:
        log.exception("Could not read state file")
        return set()

def save_state(seen):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"seen": list(seen)[-3000:]}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)

def telegram(method, data=None, files=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    response = requests.post(url, data=data or {}, files=files, timeout=60)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")
    return payload.get("result")

def send_text(text):
    if not text:
        return
    # Telegram message text max is 4096 characters; split safely.
    for start in range(0, len(text), 3900):
        telegram("sendMessage", {"chat_id": CHAT_ID, "text": text[start:start+3900], "disable_web_page_preview": "true"})

def send_media(url, kind, caption=""):
    # First try passing the source URL directly to Telegram. Some Eitaa URLs
    # require cookies/referers and won't work; in that case try downloading.
    method = {"photo": "sendPhoto", "video": "sendVideo", "document": "sendDocument"}.get(kind, "sendDocument")
    try:
        telegram(method, {"chat_id": CHAT_ID, kind: url, "caption": caption[:900]})
        return True
    except Exception as e:
        log.warning("URL media send failed (%s); trying download: %s", kind, e)

    try:
        r = SESSION.get(url, timeout=TIMEOUT, stream=True, headers={"Referer": SOURCE_URL})
        r.raise_for_status()
        content_type = r.headers.get("Content-Type", "").lower()
        if "text/html" in content_type:
            log.warning("Media URL returned HTML, not a file: %s", url)
            return False
        blob = r.content
        if not blob:
            return False
        filename = Path(urlparse(url).path).name or "attachment.bin"
        field = {"photo": "photo", "video": "video", "document": "document"}[kind]
        telegram(method, {"chat_id": CHAT_ID, "caption": caption[:900]}, files={field: (filename, blob)})
        return True
    except Exception:
        log.exception("Could not transfer media %s", url)
        return False

def deliver(message):
    text = message["text"]
    media = message["media"]
    if not media:
        send_text(text)
        return

    # Send text once, then media. Caption is attached to first media where possible.
    first = True
    for url, kind in media:
        caption = text if first else ""
        if send_media(url, kind, caption):
            first = False
    if first and text:
        # Media failed, preserve the text rather than dropping the whole post.
        send_text(text + "\n\n(انتقال رسانه ناموفق بود)")

def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in GitHub Actions Secrets.")
    html = get_page()
    messages = extract_messages(html)
    log.info("Extracted %d candidate messages from %s", len(messages), SOURCE_URL)
    if not messages:
        raise SystemExit("No message containers detected. Do not assume success; inspect the HTML parser.")

    seen = load_state()
    if seen is None:
        # Safe first run: baseline visible posts so the bot doesn't flood old history.
        save_state({m["id"] for m in messages})
        log.info("First run initialized state with %d visible messages; nothing sent.", len(messages))
        return

    # Stable message IDs are preferred; fingerprints are fallback.
    for message in messages:
        if message["id"] in seen:
            continue
        log.info("Forwarding new item %s", message["id"])
        deliver(message)
        seen.add(message["id"])
        save_state(seen)
        time.sleep(1)

if __name__ == "__main__":
    main()
