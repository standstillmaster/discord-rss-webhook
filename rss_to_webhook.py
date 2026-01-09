import os
import json
import time
import hashlib
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

STATE_FILE = "state.json"


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_url(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "github-actions-rss-webhook/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_feed(xml_bytes: bytes) -> list[dict]:
    """
    Returns list of items in newest-first order (best-effort).
    Supports RSS 2.0 and Atom feeds (enough for Steam news).
    Each item dict: {title, link, guid}
    """
    root = ET.fromstring(xml_bytes)

    # Atom
    if root.tag.endswith("feed"):
        ns = {"a": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
        items = []
        entries = root.findall("a:entry", ns) if ns else root.findall("entry")

        for entry in entries:
            title = (entry.findtext("a:title", default="", namespaces=ns) if ns else entry.findtext("title", default="")).strip()

            link = ""
            link_el = entry.find("a:link", ns) if ns else entry.find("link")
            if link_el is not None:
                link = (link_el.attrib.get("href", "") or "").strip()

            guid = (entry.findtext("a:id", default="", namespaces=ns) if ns else entry.findtext("id", default="")).strip()

            if title and link:
                items.append({"title": title, "link": link, "guid": guid or link or title})

        return items  # usually newest-first

    # RSS
    channel = root.find("channel")
    if channel is None:
        # namespace fallback
        for child in root.iter():
            if child.tag.endswith("channel"):
                channel = child
                break

    items = []
    if channel is not None:
        for item in channel:
            if not item.tag.endswith("item"):
                continue
            title = (item.findtext("title", default="") or "").strip()
            link = (item.findtext("link", default="") or "").strip()
            guid = (item.findtext("guid", default="") or "").strip()

            if title and link:
                items.append({"title": title, "link": link, "guid": guid or link or title})

    return items  # often newest-first


def stable_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def post_to_discord_webhook(
    webhook_url: str,
    username: str,
    avatar_url: str,
    title: str,
    link: str,
) -> None:
    # Discord sometimes likes a more "normal" request (helps avoid 1010)
    if "?" in webhook_url:
        url = webhook_url + "&wait=true"
    else:
        url = webhook_url + "?wait=true"

    # Optional styling from env (safe defaults)
    embed_color = int(os.environ.get("EMBED_COLOR", "16711680"))  # default red
    author_name = os.environ.get("AUTHOR_NAME", username)
    author_icon = os.environ.get("AUTHOR_ICON_URL", avatar_url)  # can be different from webhook avatar
    thumbnail_url = os.environ.get("THUMBNAIL_URL", "")          # e.g. DayZ logo direct image URL
    footer_text = os.environ.get("FOOTER_TEXT", "Kareivių Nuotykiai • Automatinės naujienos")

    embed = {
        "title": title[:256],
        "url": link,
        "color": embed_color,
        "description": "📰 Nauja žinutė iš fronto. Spausk pavadinimą ir skaityk detales.",
        "footer": {"text": footer_text},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "author": {"name": author_name},
    }

    if author_icon:
        embed["author"]["icon_url"] = author_icon

    if thumbnail_url:
        embed["thumbnail"] = {"url": thumbnail_url}

    payload = {
        "username": username,
        "embeds": [embed],
    }
    if avatar_url:
        payload["avatar_url"] = avatar_url

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; KareiviuNaujienos/1.0; +https://github.com/)",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise SystemExit(f"Discord webhook error: {e.code} {e.reason}\n{body}")



def main() -> None:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    rss_url = os.environ.get("RSS_URL", "").strip()

    if not webhook_url:
        raise SystemExit("Missing DISCORD_WEBHOOK_URL (GitHub Secret).")
    if not rss_url:
        raise SystemExit("Missing RSS_URL env var.")

    webhook_name = os.environ.get("WEBHOOK_NAME", "Būrio Ryšininkas").strip()
    avatar_url = os.environ.get("AVATAR_URL", "").strip()

    max_posts = int(os.environ.get("MAX_POSTS", "3"))
    post_on_first_run = os.environ.get("POST_ON_FIRST_RUN", "0") == "1"

    state = load_state()
    feed_key = stable_id(rss_url)
    last_seen_guid = state.get(feed_key, "")

    xml_bytes = fetch_url(rss_url)
    items = parse_feed(xml_bytes)

    if not items:
        print("No items found in feed.")
        return

    newest = items[0]
    newest_guid = newest["guid"]

    # First ever run for this feed: remember newest and optionally post once (test mode)
    if not last_seen_guid:
        state[feed_key] = newest_guid
        save_state(state)

        if post_on_first_run:
            post_to_discord_webhook(
                webhook_url=webhook_url,
                username=webhook_name,
                avatar_url=avatar_url,
                title=newest["title"],
                link=newest["link"],
            )
            print("First run test: posted newest item once.")
        else:
            print("First run: saved newest item, not posting old entries.")
        return

    # Collect items that are newer than last_seen_guid
    # Items are usually newest-first, so we walk from oldest->newest to post in order.
    to_post = []
    for it in reversed(items):
        gid = it["guid"]
        if gid == last_seen_guid:
            to_post = []  # everything before this is already posted
            continue
        to_post.append(it)

    if not to_post:
        print("No new items.")
        return

    # Limit flood
    if len(to_post) > max_posts:
        to_post = to_post[-max_posts:]

    for it in to_post:
        try:
            post_to_discord_webhook(
                webhook_url=webhook_url,
                username=webhook_name,
                avatar_url=avatar_url,
                title=it["title"],
                link=it["link"],
            )
            time.sleep(1.2)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            raise SystemExit(f"Webhook post failed: {e.code} {e.reason}\n{body}")

    # Update state to newest
    state[feed_key] = newest_guid
    save_state(state)
    print(f"Posted {len(to_post)} item(s). Updated state.")


if __name__ == "__main__":
    main()
