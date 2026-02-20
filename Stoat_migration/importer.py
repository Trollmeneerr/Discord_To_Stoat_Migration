import asyncio
import aiohttp
import sqlite3
import os
import re
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Load environment variables from Stoat_migration/.env
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# ── Config ──────────────────────────────────────────────────────────────────
STOAT_TOKEN    = os.getenv("STOAT_TOKEN")       
STOAT_SERVER   = os.getenv("STOAT_SERVER_ID")   
STOAT_API      = "https://api.stoat.chat"       
AUTUMN_API     = None                            
DELAY          = 0.8 # (rate limit buffer)
AUTHOR_HEADER_WINDOW = timedelta(minutes=5)
REPLY_PREVIEW_MAX_CHARS = 15

# link format template for automatic message link redirect
STOAT_LINK_TEMPLATE = "https://stoat.chat/server/{server}/channel/{channel}/{message}"
DISCORD_LINK_RE = re.compile(
    r"https://discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)"
)
DISCORD_USER_MENTION_RE = re.compile(r"<@!?(\d+)>")

# ────────────────────────────────────────────────────────────────────────────


def resolve_db_path():
    """
    Resolve the Discord archive DB path.
    Priority:
    1) DISCORD_ARCHIVE_DB_PATH
    2) legacy Discord_scrape/discord_archive.db
    3) newest Discord_scrape/archives/*/discord_archive.db
    """
    configured_path = (os.getenv("DISCORD_ARCHIVE_DB_PATH") or "").strip()
    if configured_path:
        candidate = Path(configured_path)
        if not candidate.is_absolute():
            candidate = (BASE_DIR / configured_path).resolve()
        if candidate.exists():
            return str(candidate)
        raise RuntimeError(
            f"DISCORD_ARCHIVE_DB_PATH is set but file does not exist: {candidate}"
        )

    candidates = []

    legacy_path = (BASE_DIR / "../Discord_scrape/discord_archive.db").resolve()
    if legacy_path.exists():
        candidates.append(legacy_path)

    archives_root = (BASE_DIR / "../Discord_scrape/archives").resolve()
    if archives_root.exists():
        archive_candidates = sorted(
            archives_root.glob("*/discord_archive.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        candidates.extend(archive_candidates)

    # De-duplicate while preserving order.
    deduped = []
    seen = set()
    for path in candidates:
        as_str = str(path)
        if as_str in seen:
            continue
        seen.add(as_str)
        deduped.append(path)

    if len(deduped) == 1:
        return str(deduped[0])
    if len(deduped) > 1:
        return str(choose_db_from_menu(deduped))

    raise RuntimeError(
        "No Discord archive database found. Set DISCORD_ARCHIVE_DB_PATH in "
        "Stoat_migration/.env or run Discord_scrape/bot.py first."
    )


def choose_db_from_menu(db_paths):
    """Simple terminal menu to choose which archive DB to import."""
    print("\n[SELECT] Multiple archive databases found. Choose one to import:")
    for idx, db_path in enumerate(db_paths, start=1):
        try:
            shown = db_path.relative_to(BASE_DIR.parent)
        except ValueError:
            shown = db_path
        print(f"  {idx}. {shown}")

    while True:
        raw = input("Enter database number (or 'q' to quit): ").strip().lower()

        if raw in {"q", "quit", "exit"}:
            raise SystemExit("Aborted by user.")

        if raw.isdigit():
            index = int(raw)
            if 1 <= index <= len(db_paths):
                selected = db_paths[index - 1]
                print(f"[SELECT] Using DB: {selected}")
                return selected

        print("Invalid selection. Enter a listed number.")

def format_message_timestamp(raw_timestamp):
    """Format DB timestamp to DD/MM/YYYY HH:MM."""
    if not raw_timestamp:
        return "00/00/0000 00:00"

    try:
        dt = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        return dt.strftime("%d-%m-%Y %H:%M")
    except ValueError:
        # Fallback keeps predictable width even for unexpected timestamp formats.
        return raw_timestamp[:16].replace("T", " ")

def parse_message_timestamp(raw_timestamp):
    """Parse DB timestamp for time-window grouping logic."""
    if not raw_timestamp:
        return None
    try:
        return datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None

DB_PATH = resolve_db_path()


def get_db():
    """Open and return a connection to the Discord archive database."""
    return sqlite3.connect(DB_PATH)


def load_user_lookup():
    """
    Build a Discord user ID -> username lookup from the archive database.
    """
    conn = get_db()
    c = conn.cursor()
    rows = c.execute("SELECT id, username, display_name FROM users").fetchall()
    conn.close()

    user_lookup = {}
    for user_id, username, display_name in rows:
        # Prefer username for stable @Username output; fallback to display_name.
        user_lookup[str(user_id)] = username or display_name or "unknown-user"
    return user_lookup


def replace_discord_user_mentions(content, user_lookup):
    """
    Replace Discord user mention tokens (<@123> / <@!123>) with @username.
    """
    if not content:
        return ""

    def _replace(match):
        user_id = match.group(1)
        username = user_lookup.get(user_id)
        return f"@{username}" if username else match.group(0)

    return DISCORD_USER_MENTION_RE.sub(_replace, content)


def get_table_columns(cursor, table_name):
    """
    Return all column names for a SQLite table.
    """
    return {row[1] for row in cursor.execute(f"PRAGMA table_info({table_name})").fetchall()}


def build_preview_text(text, max_chars):
    """
    Normalize whitespace and return a capped preview string.
    """
    if not text:
        return ""
    compact = " ".join(text.split())
    return compact[:max_chars]


def format_reply_context(msg, user_lookup):
    """
    Build one-line reply context from archived metadata.
    """
    reply_message_id = msg.get("reply_to_message_id")
    if not reply_message_id:
        return ""

    author = (
        msg.get("reply_to_author_username")
        or user_lookup.get(str(msg.get("reply_to_author_id") or ""))
        or "unknown-user"
    )
    raw_content = replace_discord_user_mentions(msg.get("reply_to_content") or "", user_lookup)
    preview = build_preview_text(raw_content, REPLY_PREVIEW_MAX_CHARS) or "[no text]"
    return f"> Reply to @{author}: {preview}"


async def fetch_autumn_url(session):
    """
    Fetch the correct file upload (Autumn) URL from the Stoat API root endpoint.
    This avoids hardcoding a URL that may change, as Stoat recently migrated CDNs.
    Returns the Autumn base URL string, or raises if it cannot be found.
    """
    async with session.get(f"{STOAT_API}/") as resp:
        if resp.status == 200:
            data = await resp.json()
            # The API root returns feature URLs under features.autumn.url
            autumn_url = data.get("features", {}).get("autumn", {}).get("url")
            if autumn_url:
                print(f"[CONFIG] Autumn upload URL: {autumn_url}")
                return autumn_url.rstrip("/")
        raise RuntimeError(f"Could not fetch Autumn URL from Stoat API root: {resp.status}")


async def create_channel(session, headers, channel_name, channel_type="text"):
    """
    Create a Text or Voice channel on the Stoat server.
    Returns the new channel's ID, or None on failure.
    """
    url = f"{STOAT_API}/servers/{STOAT_SERVER}/channels"
    stoat_type = "Voice" if channel_type == "voice" else "Text"
    label = "🔊" if channel_type == "voice" else "#"

    payload = {
        "type": stoat_type,
        "name": channel_name[:32],
        "description": f"Imported from Discord {label}{channel_name}"
    }
    async with session.post(url, json=payload, headers=headers) as resp:
        if resp.status in (200, 201):
            data = await resp.json()
            return data.get("_id") or data.get("id")
        else:
            text = await resp.text()
            print(f"  [WARN] Could not create {label}{channel_name}: {resp.status} {text}")
            return None


async def download_to_temp(session, url, filename):
    """
    Download a file from a URL into a local temp folder.
    Returns the local filepath on success, or None on failure.
    Used when the archiver did not save the file locally.
    """
    temp_dir = BASE_DIR / "temp_downloads"
    temp_dir.mkdir(parents=True, exist_ok=True)
    safe_name = filename.replace("/", "_").replace("\\", "_")
    filepath = temp_dir / safe_name

    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                with open(filepath, "wb") as f:
                    f.write(await resp.read())
                print(f"  [DL] Downloaded {filename} from Discord CDN")
                return str(filepath)
            else:
                print(f"  [WARN] Could not download {filename}: {resp.status}")
                return None
    except Exception as e:
        print(f"  [WARN] Download error for {filename}: {e}")
        return None


async def upload_file(session, headers, filepath, filename, autumn_url):
    """
    Upload a file to Stoat's Autumn file server.
    Returns the file ID on success, or None on failure.
    """
    if not filepath or not os.path.exists(filepath):
        return None

    url = f"{autumn_url}/attachments"

    # Strip Content-Type header for multipart — aiohttp sets it automatically
    upload_headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}

    with open(filepath, "rb") as f:
        form = aiohttp.FormData()
        form.add_field("file", f, filename=filename)
        async with session.post(url, data=form, headers=upload_headers) as resp:
            if resp.status in (200, 201):
                data = await resp.json()
                return data.get("id")
            else:
                text = await resp.text()
                print(f"  [WARN] Upload failed for {filename}: {resp.status} {text}")
                return None


async def send_message(session, headers, channel_id, content, attachment_ids=None):
    """
    Send a message to a Stoat channel.
    Returns the Stoat message ID on success, or None on failure.
    """
    url = f"{STOAT_API}/channels/{channel_id}/messages"
    payload = {"content": content}

    if attachment_ids:
        payload["attachments"] = attachment_ids

    async with session.post(url, json=payload, headers=headers) as resp:
        if resp.status in (200, 201):
            data = await resp.json()
            # Return the Stoat message ID so we can map it
            return data.get("_id") or data.get("id")
        else:
            text = await resp.text()
            print(f"  [WARN] Message send failed: {resp.status} {text}")
            return None


async def edit_message(session, headers, channel_id, stoat_message_id, new_content):
    """
    Edit an already-sent Stoat message to replace Discord links with Stoat links.
    """
    url = f"{STOAT_API}/channels/{channel_id}/messages/{stoat_message_id}"
    payload = {"content": new_content}

    async with session.patch(url, json=payload, headers=headers) as resp:
        if resp.status not in (200, 201):
            text = await resp.text()
            print(f"  [WARN] Edit failed for message {stoat_message_id}: {resp.status} {text}")


async def import_channel(session, headers, discord_channel_id, discord_channel_name,
                         channel_type, discord_to_stoat_msg, discord_to_stoat_channel,
                         autumn_url, user_lookup):
    """
    Import one channel into Stoat.
    Populates discord_to_stoat_msg with {discord_msg_id: (stoat_channel_id, stoat_msg_id)}
    so the redirect pass can build correct Stoat links.
    """
    conn = get_db()
    c = conn.cursor()

    label = "🔊" if channel_type == "voice" else "#"
    print(f"\n[→] Creating Stoat {channel_type} channel: {label}{discord_channel_name}")

    stoat_channel_id = await create_channel(session, headers, discord_channel_name, channel_type)
    if not stoat_channel_id:
        print(f"  [SKIP] Skipping {label}{discord_channel_name}")
        conn.close()
        return

    # Store the Discord→Stoat channel mapping
    discord_to_stoat_channel[discord_channel_id] = stoat_channel_id

    message_columns = get_table_columns(c, "messages")
    has_reply_metadata = {
        "reply_to_message_id",
        "reply_to_channel_id",
        "reply_to_author_id",
        "reply_to_author_username",
        "reply_to_content",
    }.issubset(message_columns)

    # Fetch all messages for this channel, oldest first.
    if has_reply_metadata:
        rows = c.execute(
            """
            SELECT
                m.id, m.author_id, m.content, m.timestamp, u.username,
                m.reply_to_message_id, m.reply_to_channel_id, m.reply_to_author_id,
                m.reply_to_author_username, m.reply_to_content
            FROM messages m
            JOIN users u ON m.author_id = u.id
            WHERE m.channel_id = ?
            ORDER BY m.timestamp ASC
            """,
            (discord_channel_id,),
        ).fetchall()
    else:
        rows = c.execute(
            """
            SELECT m.id, m.author_id, m.content, m.timestamp, u.username
            FROM messages m
            JOIN users u ON m.author_id = u.id
            WHERE m.channel_id = ?
            ORDER BY m.timestamp ASC
            """,
            (discord_channel_id,),
        ).fetchall()

    messages = {}
    for row in rows:
        if has_reply_metadata:
            (
                msg_id,
                author_id,
                content,
                timestamp,
                username,
                reply_to_message_id,
                reply_to_channel_id,
                reply_to_author_id,
                reply_to_author_username,
                reply_to_content,
            ) = row
        else:
            msg_id, author_id, content, timestamp, username = row
            reply_to_message_id = None
            reply_to_channel_id = None
            reply_to_author_id = None
            reply_to_author_username = None
            reply_to_content = None

        messages[msg_id] = {
            "author_id": author_id,
            "username": username,
            "content": content or "",
            "timestamp": timestamp,
            "attachments": [],
            "reply_to_message_id": reply_to_message_id,
            "reply_to_channel_id": reply_to_channel_id,
            "reply_to_author_id": reply_to_author_id,
            "reply_to_author_username": reply_to_author_username,
            "reply_to_content": reply_to_content,
        }

    attachment_rows = c.execute(
        """
        SELECT a.message_id, a.local_path, a.filename, a.url, a.content_type, a.size
        FROM attachments a
        JOIN messages m ON m.id = a.message_id
        WHERE m.channel_id = ?
        ORDER BY a.id ASC
        """,
        (discord_channel_id,),
    ).fetchall()

    for message_id, local_path, filename, url, content_type, size in attachment_rows:
        if not filename or message_id not in messages:
            continue
        messages[message_id]["attachments"].append(
            {
                "local_path": local_path,
                "filename": filename,
                "url": url,
                "content_type": content_type,
                "size": size or 0,
            }
        )

    if not messages:
        print(f"  [INFO] No messages in {label}{discord_channel_name}")
        conn.close()
        return

    print(f"  [~] Sending {len(messages)} messages...")
    count = 0

    last_author_id = None
    last_message_time = None

    for discord_msg_id, msg in messages.items():
        message_time = parse_message_timestamp(msg["timestamp"])
        author_id = msg["author_id"]
        show_header = True

        if (
            author_id == last_author_id
            and message_time is not None
            and last_message_time is not None
            and message_time >= last_message_time
            and (message_time - last_message_time) <= AUTHOR_HEADER_WINDOW
        ):
            show_header = False

        date_str = format_message_timestamp(msg["timestamp"])

        # Message header with fixed spacing between username and date.
        author_name = msg["username"] or "unknown-user"
        header = f"``{author_name} at: {date_str}``" if show_header else ""
        body = replace_discord_user_mentions(msg["content"], user_lookup)
        reply_context = format_reply_context(msg, user_lookup)


        # Process attachments — upload directly from local file,
        # or download from Discord CDN first if not saved locally, then upload.
        # Only fall back to a link if both methods fail.
        attachment_ids = []
        extra_links = []

        for att in msg["attachments"]:
            # Resolve local_path relative to the Discord folder
            raw_path = att["local_path"]
            if raw_path:
                # Try as-is first, then relative to the Discord folder
                if os.path.exists(raw_path):
                    filepath = raw_path
                else:
                    discord_path = os.path.join("../Discord", raw_path)
                    filepath = discord_path if os.path.exists(discord_path) else None
            else:
                filepath = None

            # No local file — download it from Discord CDN first
            if not filepath and att["url"]:
                print(f"  [DL] No local file for {att['filename']}, downloading from Discord CDN...")
                filepath = await download_to_temp(session, att["url"], att["filename"])

            if filepath:
                file_id = await upload_file(session, headers, filepath, att["filename"], autumn_url)
                if file_id:
                    attachment_ids.append(file_id)
                    continue

            # Both local and CDN failed — last resort: post the URL as text
            if att["url"]:
                print(f"  [FALLBACK] Could not upload {att['filename']}, posting link as last resort")
                extra_links.append(f"📎 {att['filename']}: {att['url']}")

        # Build full message — Discord links are left as-is for now,
        # the redirect pass will rewrite them after all messages are sent
        full_parts = [
            part
            for part in (
                header,
                reply_context,
                body,
                "\n".join(extra_links),
            )
            if part
        ]
        full_message = "\n".join(full_parts)

        # Stoat 2000 char limit
        if len(full_message) > 2000:
            full_message = full_message[:1997] + "..."

        stoat_msg_id = await send_message(
            session, headers, stoat_channel_id, full_message, attachment_ids or None
        )

        # Record the Discord→Stoat message ID mapping for the redirect pass
        if stoat_msg_id:
            discord_to_stoat_msg[discord_msg_id] = (stoat_channel_id, stoat_msg_id)

        count += 1
        last_author_id = author_id
        last_message_time = message_time
        await asyncio.sleep(DELAY)

        if count % 50 == 0:
            print(f"  [~] {count}/{len(messages)} messages sent...")

    conn.close()
    print(f"  [✓] {label}{discord_channel_name} done — {count} messages imported")


async def fix_redirects(session, headers, discord_to_stoat_msg, discord_to_stoat_channel):
    """
    Pass 2 — after ALL messages are sent:
    Find every message that contained a Discord link, build the correct Stoat link,
    and edit the Stoat message to replace it.
    """
    conn = get_db()
    c = conn.cursor()

    # Fetch all recorded redirects from the archiver
    redirects = c.execute("""
        SELECT DISTINCT source_message_id, linked_channel_id, linked_message_id, original_url
        FROM redirects
    """).fetchall()
    conn.close()

    if not redirects:
        print("\n[REDIRECTS] No message links to fix.")
        return

    print(f"\n[REDIRECTS] Fixing {len(redirects)} message link(s)...")
    fixed = 0
    skipped = 0

    # Group redirects by source message so we only edit each message once
    by_source = {}
    for source_msg_id, linked_channel_id, linked_msg_id, original_url in redirects:
        by_source.setdefault(source_msg_id, []).append(
            (linked_channel_id, linked_msg_id, original_url)
        )

    for source_discord_msg_id, links in by_source.items():

        # Find the Stoat message that corresponds to this Discord message
        if source_discord_msg_id not in discord_to_stoat_msg:
            print(f"  [SKIP] Source message {source_discord_msg_id} was not imported")
            skipped += len(links)
            continue

        stoat_channel_id, stoat_msg_id = discord_to_stoat_msg[source_discord_msg_id]

        # Fetch the current content of that Stoat message so we can do string replacement
        get_url = f"{STOAT_API}/channels/{stoat_channel_id}/messages/{stoat_msg_id}"
        async with session.get(get_url, headers=headers) as resp:
            if resp.status != 200:
                print(f"  [WARN] Could not fetch Stoat message {stoat_msg_id}")
                skipped += len(links)
                continue
            data = await resp.json()
            current_content = data.get("content", "")

        # Replace each Discord link in this message with the Stoat equivalent
        new_content = current_content
        for linked_channel_id, linked_msg_id, original_url in links:

            if linked_msg_id in discord_to_stoat_msg:
                # We have the Stoat message ID — build a proper deep link
                target_stoat_channel, target_stoat_msg = discord_to_stoat_msg[linked_msg_id]
                stoat_link = STOAT_LINK_TEMPLATE.format(
                    server=STOAT_SERVER,
                    channel=target_stoat_channel,
                    message=target_stoat_msg
                )
                new_content = new_content.replace(original_url, stoat_link)
                print(f"  [✓] Rewrote link → {stoat_link}")
                fixed += 1
            else:
                # Target message wasn't imported (maybe a different server or deleted)
                # Leave a note in place of the broken link
                note = f"[Linked message not imported — original: {original_url}]"
                new_content = new_content.replace(original_url, note)
                print(f"  [WARN] Target {linked_msg_id} not found, added note")
                skipped += 1

        # Edit the Stoat message with the rewritten content
        if new_content != current_content:
            await edit_message(session, headers, stoat_channel_id, stoat_msg_id, new_content)
            await asyncio.sleep(DELAY)

    print(f"[REDIRECTS] Done — {fixed} link(s) rewritten, {skipped} skipped.")


async def main():
    """
    Entry point:
    Pass 1 — Import all channels and messages, track Discord→Stoat ID mapping.
    Pass 2 — Rewrite all Discord message links to Stoat links.
    """
    if not STOAT_TOKEN or not STOAT_SERVER:
        print("[ERROR] Set STOAT_TOKEN and STOAT_SERVER_ID environment variables first!")
        return

    print(f"[CONFIG] Using archive DB: {DB_PATH}")

    headers = {
        "x-bot-token": STOAT_TOKEN,
        "Content-Type": "application/json"
    }

    conn = get_db()
    channels = conn.execute("SELECT id, name, type FROM channels").fetchall()
    conn.close()

    text_channels  = [(id, name, t) for id, name, t in channels if t == "text"]
    voice_channels = [(id, name, t) for id, name, t in channels if t == "voice"]

    print(f"[START] {len(text_channels)} text + {len(voice_channels)} voice channels to import")
    print(f"[START] Target Stoat server: {STOAT_SERVER}\n")

    user_lookup = load_user_lookup()
    print(f"[CONFIG] Loaded {len(user_lookup)} users for mention replacement")

    # Shared maps populated during Pass 1, consumed during Pass 2
    discord_to_stoat_msg     = {}  # discord_msg_id  → (stoat_channel_id, stoat_msg_id)
    discord_to_stoat_channel = {}  # discord_chan_id → stoat_channel_id

    async with aiohttp.ClientSession() as session:

        # Fetch the correct Autumn (file upload) URL from the Stoat API
        autumn_url = await fetch_autumn_url(session)

        # ── PASS 1: Import everything ─────────────────────────────────────
        print("━━━ PASS 1: IMPORTING MESSAGES ━━━")

        print("\n[TEXT CHANNELS]")
        for channel_id, channel_name, channel_type in text_channels:
            await import_channel(
                session, headers, channel_id, channel_name, channel_type,
                discord_to_stoat_msg, discord_to_stoat_channel, autumn_url, user_lookup
            )

        print("\n[VOICE CHANNELS]")
        for channel_id, channel_name, channel_type in voice_channels:
            await import_channel(
                session, headers, channel_id, channel_name, channel_type,
                discord_to_stoat_msg, discord_to_stoat_channel, autumn_url, user_lookup
            )

        # ── PASS 2: Fix all message link redirects ────────────────────────
        print("\n━━━ PASS 2: FIXING MESSAGE LINKS ━━━")
        await fix_redirects(session, headers, discord_to_stoat_msg, discord_to_stoat_channel)

    print("\n[DONE] Import complete!")


asyncio.run(main())
