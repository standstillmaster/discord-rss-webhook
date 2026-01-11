import os
import json
import time
import hashlib
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import re

STATE_FILE = "state.json"


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_url(url, timeout=20):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; KareiviuRSS/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)

    # Atom feeds
    if root.tag.endswith("feed"):
        ns = {"a": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
        items = []
        entries = root.findall("a:entry", ns) if ns else root.findall("entry")
        for e in entries:
            title = (e.findtext("a:title", default="", namespaces=ns) if ns else e.findtext("title", default="")).strip()
            link_el = e.find("a:link", ns) if ns else e.find("link")
            link = link_el.attrib.get("href", "").strip() if link_el is not None else ""
            guid = (e.findtext("a:id", default="", namespaces=ns) if ns else e.findtext("id", default="")).strip()
            if title and link:
                items.append({"title": title, "link": link, "guid": guid or link})
        return items

    # RSS feeds
    channel = root.find("channel")
    if channel is None:
        for c in root.iter():
            if c.tag.endswith("channel"):
                channel = c
                break

    items = []
    if channel is not None:
        for it in channel:
            if not it.tag.endswith("item"):
                continue
            title = (it.findtext("title", default="") or "").strip()
            link = (it.findtext("link", default="") or "").strip()
            guid = (it.findtext("guid", default="") or "").strip()
            if title and link:
                items.append({"title": title, "link": link, "guid": guid or link})
    return items


def extract_og_image_url(page_url):
    try:
        req = urllib.request.Request(
            page_url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; KareiviuRSS/1.0)",
                "Accept": "text/html",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return ""

    m = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        flags=re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()

    m = re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        html,
        flags=re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()

    return ""


def post_to_discord_webhook(webhook_url, username, avatar_url, title, link):
    if "?" in webhook_url:
        url = webhook_url + "&wait=true"
    else:
        url = webhook_url + "?wait=true"

    embed = {
        "title": title[:256],
        "url": link,
        "description": "📰 Nauja žinutė iš štabo.",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    og_image = extract_og_image_url(link)
    if og_image:
        embed["image"] = {"url": og_image}

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
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; KareiviuRSS/1.0)",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def stable_id(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def main():
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    rss_url = os.environ.get("RSS_URL", "").strip()
    webhook_name = os.environ.get("WEBHOOK_NAME", "Būrio Ryšininkas").strip()
    avatar_url = os.environ.get("AVATAR_URL", "").strip()

    max_posts = int(os.environ.get("MAX_POSTS", "3"))
    force_test = os.environ.get("FORCE_TEST", "0") == "1"

    if not webhook_url or not rss_url:
        print("Missing env vars.")
        return

    xml = fetch_url(rss_url)
    items = parse_feed(xml)

    # FORCE TEST — veikia visada
    if force_test:
        if items:
            newest = items[0]
            post_to_discord_webhook(
                webhook_url, webhook_name, avatar_url,
                newest["title"], newest["link"]
            )
            print("Force test post sent.")
        else:
            print("Force test: feed empty.")
        return

    if not items:
        print("No items.")
        return

    state = load_state()
    feed_key = stable_id(rss_url)
    last_seen = state.get(feed_key, "")

    newest = items[0]

    if not last_seen:
        state[feed_key] = newest["guid"]
        save_state(state)
        print("First run, state saved.")
        return

    to_post = []
    for it in reversed(items):
        if it["guid"] == last_seen:
            to_post = []
            continue
        to_post.append(it)

    if not to_post:
        print("No new items.")
        return

    if len(to_post) > max_posts:
        to_post = to_post[-max_posts:]

    for it in to_post:
        post_to_discord_webhook(
            webhook_url, webhook_name, avatar_url,
            it["title"], it["link"]
        )
        time.sleep(1)

    state[feed_key] = newest["guid"]
    save_state(state)
    print(f"Posted {len(to_post)} items.")


if __name__ == "__main__":
    main()
