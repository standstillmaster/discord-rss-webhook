import os
import json
import time
import hashlib
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

STATE_FILE = "state.json"

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def fetch_url(url: str, timeout=20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "github-actions-rss-webhook/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()

def parse_rss_items(xml_bytes: bytes):
    root = ET.fromstring(xml_bytes)

    # Atom
    if root.tag.endswith("feed"):
        ns = {"a": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
        items = []
        for entry in root.findall("a:entry", ns) if ns else root.findall("entry"):
            title = (entry.findtext("a:title", default="", namespaces=ns) if ns else entry.findtext("title", default="")).strip()
            link_el = (entry.find("a:link", ns) if ns else entry.find("link"))
            link = ""
            if link_el is not None:
                link = link_el.attrib.get("href", "").strip()
            guid = (entry.findtext("a:id", default="", namespaces=ns) if ns else entry.findtext("id", default="")).strip()
            published = (entry.findtext("a:updated", default="", namespaces=ns) if ns else entry.findtext("updated", default="")).strip()
            items.append({"title": title, "link": link, "guid": guid or link or title, "published": published})
        return items

    # RSS
    channel = root.find("channel")
    if channel is None:
        for child in root.iter():
            if child.tag.endswith("channel"):
                channel = child
                break

    items = []
    if channel is not None:
        for item in list(channel):
            if not item.tag.endswith("item"):
                continue
            title = (item.findtext("title", default="") or "").strip()
            link = (item.findtext("link", default="") or "").strip()
            guid = (item.findtext("guid", default="") or "").strip()
            pub = (item.findtext("pubDate", default="") or "").strip()
            items.append({"title": title, "link": link, "guid": guid or link or title, "published": pub})
    return items

def stable_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

def post_to_webhook(webhook_url: str, username: str, avatar_url: str, title: str, link: str):
    payload = {
        "username": username,
        "avatar_url": avatar_url,
        "embeds": [
            {
                "title": title[:256],
                "url": link,
                "description": link,
            }
        ]
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "github-actions-rss-webhook/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()

def main():
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        raise SystemExit("Missing DISCORD_WEBHOOK_URL env var (GitHub Secret).")

    rss_url = os.environ.get("RSS_URL", "").strip()
    if not rss_url:
        raise SystemExit("Missing RSS_URL env var.")

    webhook_name = os.environ.get("WEBHOOK_NAME", "Žaidimų naujienos").strip()
    avatar_url = os.environ.get("AVATAR_URL", "").strip()

    state = load_state()
    feed_key = stable_id(rss_url)
    last_seen = state.get(feed_key, "")

    xml_bytes = fetch_url(rss_url)
    items = parse_rss_items(xml_bytes)

    items = [it for it in items if it.get("title") and it.get("link")]
    if not items:
        print("No items found.")
        return

    unseen = []
    for it in reversed(items):
        gid = it.get("guid") or it.get("link") or it.get("title")
        if last_seen and gid == last_seen:
            unseen = []
            continue
        unseen.append(it)

    newest_gid = (items[0].get("guid") or items[0].get("link") or items[0].get("title"))
    if not last_seen:
    # If you want a one-time test post on first run, set POST_ON_FIRST_RUN=1
    post_on_first = os.environ.get("POST_ON_FIRST_RUN", "0") == "1"
    state[feed_key] = newest_gid
    save_state(state)

    if not post_on_first:
        print("First run: saved newest item, not posting old entries.")
        return

    # Post newest item once (test mode)
    post_to_webhook(
        webhook_url=webhook_url,
        username=webhook_name,
        avatar_url=avatar_url,
        title=items[0]["title"],
        link=items[0]["link"],
    )
    print("First run test: posted newest item once.")
    return


    MAX_POSTS = int(os.environ.get("MAX_POSTS", "3"))
    to_post = unseen[-MAX_POSTS:] if len(unseen) > MAX_POSTS else unseen

    for it in to_post:
        try:
            post_to_webhook(
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

    state[feed_key] = newest_gid
    save_state(state)
    print(f"Posted {len(to_post)} item(s). Updated last_seen.")

if __name__ == "__main__":
    main()
