import asyncio
import os
import re
import sqlite3
from pathlib import Path
import aiohttp
import discord
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip()
ARCHIVES_ROOT = BASE_DIR / "archives"

DISCORD_LINK_RE = re.compile(
    r"https://discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)"
)


def parse_message_limit(raw_value):
    value = (raw_value or "").strip().lower()
    if not value or value == "none":
        return None
    try:
        parsed = int(value)
        if parsed <= 0:
            raise ValueError
        return parsed
    except ValueError as exc:
        raise RuntimeError(
            "DISCORD_MESSAGE_LIMIT must be a positive integer or 'none'. "
            f"Received: {raw_value!r}"
        ) from exc


MESSAGE_LIMIT = parse_message_limit(os.getenv("DISCORD_MESSAGE_LIMIT"))

# Setup intents - message content and guild/member visibility are required.
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

client = discord.Client(intents=intents)


def sanitize_for_path(name):
    """Create a filesystem-safe folder segment."""
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return sanitized or "server"


def get_output_paths(guild):
    """
    Return per-server output paths:
    Discord_scrape/archives/<server_name>_<server_id>/
      - discord_archive.db
      - downloads/
    """
    server_folder = ARCHIVES_ROOT / f"{sanitize_for_path(guild.name)}_{guild.id}"
    downloads_folder = server_folder / "downloads"
    db_path = server_folder / "discord_archive.db"

    downloads_folder.mkdir(parents=True, exist_ok=True)
    return server_folder, db_path, downloads_folder


def choose_guild_from_menu(guilds):
    """Simple terminal menu to choose one guild."""
    if not guilds:
        raise RuntimeError("Bot is not in any servers.")

    if len(guilds) == 1:
        guild = guilds[0]
        print(f"[SELECT] Only one server found. Using: {guild.name} ({guild.id})")
        return guild

    print("\n[SELECT] Bot is in multiple servers. Pick one to archive:")
    for idx, guild in enumerate(guilds, start=1):
        print(f"  {idx}. {guild.name} ({guild.id})")

    while True:
        raw = input("Enter server number (or 'q' to quit): ")
        choice = raw.strip().lower()

        if choice in {"q", "quit", "exit"}:
            raise SystemExit("Aborted by user.")

        if choice.isdigit():
            index = int(choice)
            if 1 <= index <= len(guilds):
                guild = guilds[index - 1]
                print(f"[SELECT] Using: {guild.name} ({guild.id})")
                return guild

        print("Invalid selection. Enter a listed number.")


async def select_target_guild(guilds):
    """Async wrapper so terminal input does not block the event loop."""
    return await asyncio.to_thread(choose_guild_from_menu, guilds)


def init_db(db_path):
    """Create the SQLite database and tables if they do not exist."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS guilds (
            id TEXT PRIMARY KEY,
            name TEXT
        )
    """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS channels (
            id TEXT PRIMARY KEY,
            guild_id TEXT,
            name TEXT,
            type TEXT DEFAULT 'text',
            FOREIGN KEY (guild_id) REFERENCES guilds(id)
        )
    """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT,
            display_name TEXT
        )
    """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            channel_id TEXT,
            guild_id TEXT,
            author_id TEXT,
            content TEXT,
            timestamp TEXT,
            has_attachments INTEGER DEFAULT 0,
            reply_to_message_id TEXT,
            reply_to_channel_id TEXT,
            reply_to_author_id TEXT,
            reply_to_author_username TEXT,
            reply_to_content TEXT,
            FOREIGN KEY (channel_id) REFERENCES channels(id),
            FOREIGN KEY (author_id) REFERENCES users(id)
        )
    """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS attachments (
            id TEXT PRIMARY KEY,
            message_id TEXT,
            filename TEXT,
            url TEXT,
            content_type TEXT,
            size INTEGER,
            local_path TEXT,
            FOREIGN KEY (message_id) REFERENCES messages(id)
        )
    """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS redirects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_message_id TEXT,
            linked_guild_id TEXT,
            linked_channel_id TEXT,
            linked_message_id TEXT,
            original_url TEXT
        )
    """
    )

    ensure_messages_reply_columns(conn)

    conn.commit()
    conn.close()
    print(f"[DB] Database initialized: {db_path}")


def ensure_messages_reply_columns(conn):
    """
    Backfill reply columns for existing archive DBs created before reply support.
    """
    c = conn.cursor()
    existing = {row[1] for row in c.execute("PRAGMA table_info(messages)").fetchall()}
    needed = {
        "reply_to_message_id": "TEXT",
        "reply_to_channel_id": "TEXT",
        "reply_to_author_id": "TEXT",
        "reply_to_author_username": "TEXT",
        "reply_to_content": "TEXT",
    }
    for column, definition in needed.items():
        if column in existing:
            continue
        c.execute(f"ALTER TABLE messages ADD COLUMN {column} {definition}")
        print(f"[DB] Added messages.{column}")


async def resolve_referenced_message(message):
    """
    Resolve reply target message. Prefer cached/resolved references, then fetch via API.
    """
    reference = message.reference
    if not reference or not reference.message_id:
        return None

    resolved = getattr(reference, "resolved", None)
    if isinstance(resolved, discord.Message):
        return resolved

    cached = getattr(reference, "cached_message", None)
    if isinstance(cached, discord.Message):
        return cached

    channel_id = reference.channel_id or message.channel.id

    target_channel = None
    if message.guild is not None and channel_id is not None:
        target_channel = message.guild.get_channel_or_thread(channel_id)

    if target_channel is None and str(channel_id) == str(message.channel.id):
        target_channel = message.channel

    if target_channel is None or not hasattr(target_channel, "fetch_message"):
        return None

    try:
        return await target_channel.fetch_message(reference.message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def download_attachment(session, url, filename, folder):
    """Download one attachment into the configured server folder."""
    os.makedirs(folder, exist_ok=True)
    safe_name = filename.replace("/", "_").replace("\\", "_")
    filepath = os.path.join(folder, safe_name)

    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                with open(filepath, "wb") as f:
                    f.write(await resp.read())
                return str(Path(filepath).resolve())
    except Exception as e:
        print(f"[WARN] Failed to download {filename}: {e}")
    return None


def extract_redirects(message_id, content):
    """Scan content for Discord message links and return rows to insert."""
    redirects = []
    if not content:
        return redirects

    for match in DISCORD_LINK_RE.finditer(content):
        guild_id, channel_id, linked_msg_id = match.groups()
        redirects.append(
            (
                message_id,
                guild_id,
                channel_id,
                linked_msg_id,
                match.group(0),
            )
        )
    return redirects


async def archive_channel(channel, conn, session, downloads_folder, channel_type="text"):
    """
    Fetch messages for one channel and store data in DB.
    If MESSAGE_LIMIT is set, only latest N messages are archived.
    """
    c = conn.cursor()

    c.execute(
        "INSERT OR IGNORE INTO channels VALUES (?, ?, ?, ?)",
        (str(channel.id), str(channel.guild.id), channel.name, channel_type),
    )
    conn.commit()

    label = "VOICE " if channel_type == "voice" else "#"
    print(f"  [->] Archiving {label}{channel.name} ({channel_type})...")
    count = 0
    redirect_count = 0
    reply_cache = {}

    if MESSAGE_LIMIT is None:
        print("    [CFG] Message mode: full history")
    else:
        print(f"    [CFG] Message mode: latest {MESSAGE_LIMIT}")

    async def process_message(message):
        nonlocal count, redirect_count

        c.execute(
            "INSERT OR IGNORE INTO users VALUES (?, ?, ?)",
            (
                str(message.author.id),
                str(message.author.name),
                str(message.author.display_name),
            ),
        )

        has_attachments = 1 if message.attachments else 0

        reply_to_message_id = None
        reply_to_channel_id = None
        reply_to_author_id = None
        reply_to_author_username = None
        reply_to_content = None

        if message.reference and message.reference.message_id:
            reply_to_message_id = str(message.reference.message_id)
            reply_to_channel_id = str(message.reference.channel_id or channel.id)

            cache_key = (reply_to_channel_id, reply_to_message_id)
            if cache_key in reply_cache:
                referenced_message = reply_cache[cache_key]
            else:
                referenced_message = await resolve_referenced_message(message)
                reply_cache[cache_key] = referenced_message

            if referenced_message:
                reply_to_author_id = str(referenced_message.author.id)
                reply_to_author_username = str(referenced_message.author.name)
                reply_to_content = referenced_message.content or ""
                if not reply_to_content and referenced_message.attachments:
                    reply_to_content = "[Attachment]"

        c.execute(
            """
            INSERT INTO messages (
                id, channel_id, guild_id, author_id, content, timestamp, has_attachments,
                reply_to_message_id, reply_to_channel_id, reply_to_author_id,
                reply_to_author_username, reply_to_content
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                channel_id = excluded.channel_id,
                guild_id = excluded.guild_id,
                author_id = excluded.author_id,
                content = excluded.content,
                timestamp = excluded.timestamp,
                has_attachments = excluded.has_attachments,
                reply_to_message_id = COALESCE(excluded.reply_to_message_id, messages.reply_to_message_id),
                reply_to_channel_id = COALESCE(excluded.reply_to_channel_id, messages.reply_to_channel_id),
                reply_to_author_id = COALESCE(excluded.reply_to_author_id, messages.reply_to_author_id),
                reply_to_author_username = COALESCE(excluded.reply_to_author_username, messages.reply_to_author_username),
                reply_to_content = COALESCE(excluded.reply_to_content, messages.reply_to_content)
            """,
            (
                str(message.id),
                str(channel.id),
                str(channel.guild.id),
                str(message.author.id),
                message.content,
                message.created_at.isoformat(),
                has_attachments,
                reply_to_message_id,
                reply_to_channel_id,
                reply_to_author_id,
                reply_to_author_username,
                reply_to_content,
            ),
        )

        redirects = extract_redirects(str(message.id), message.content)
        for row in redirects:
            c.execute(
                "INSERT INTO redirects (source_message_id, linked_guild_id, linked_channel_id, linked_message_id, original_url) VALUES (?, ?, ?, ?, ?)",
                row,
            )
            redirect_count += 1

        for attachment in message.attachments:
            local_path = await download_attachment(
                session=session,
                url=attachment.url,
                filename=f"{attachment.id}_{attachment.filename}",
                folder=str(downloads_folder),
            )
            c.execute(
                "INSERT OR IGNORE INTO attachments VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(attachment.id),
                    str(message.id),
                    attachment.filename,
                    attachment.url,
                    attachment.content_type or "unknown",
                    attachment.size,
                    local_path,
                ),
            )

        count += 1
        if count % 500 == 0:
            conn.commit()
            print(f"    [~] {count} messages saved...")

    try:
        if MESSAGE_LIMIT is None:
            async for message in channel.history(limit=None, oldest_first=True):
                await process_message(message)
        else:
            latest_messages = [
                message
                async for message in channel.history(
                    limit=MESSAGE_LIMIT, oldest_first=False
                )
            ]
            for message in reversed(latest_messages):
                await process_message(message)

    except discord.errors.HTTPException as e:
        # Voice channels can return 400 for empty/no text history.
        if e.status == 400:
            print(f"  [INFO] {label}{channel.name} has no text chat history, skipping.")
        else:
            raise

    conn.commit()
    print(f"  [OK] {label}{channel.name} done - {count} messages, {redirect_count} links.")


@client.event
async def on_ready():
    print(f"[BOT] Logged in as {client.user}")

    guilds = sorted(client.guilds, key=lambda g: g.name.lower())
    target_guild = await select_target_guild(guilds)

    server_folder, db_path, downloads_folder = get_output_paths(target_guild)
    print(f"[OUT] Server archive folder: {server_folder}")
    print(f"[OUT] Database: {db_path}")
    print(f"[OUT] Downloads: {downloads_folder}")

    init_db(str(db_path))
    conn = sqlite3.connect(str(db_path))

    async with aiohttp.ClientSession() as session:
        print(f"\n[GUILD] Archiving: {target_guild.name} ({target_guild.id})")
        c = conn.cursor()
        c.execute(
            "INSERT OR IGNORE INTO guilds VALUES (?, ?)",
            (str(target_guild.id), target_guild.name),
        )
        conn.commit()

        print("\n[TEXT CHANNELS]")
        for channel in target_guild.text_channels:
            try:
                await archive_channel(
                    channel=channel,
                    conn=conn,
                    session=session,
                    downloads_folder=downloads_folder,
                    channel_type="text",
                )
            except discord.Forbidden:
                print(f"  [SKIP] No access to #{channel.name}")
            except Exception as e:
                print(f"  [ERROR] #{channel.name}: {e}")

        print("\n[VOICE CHANNELS]")
        for channel in target_guild.voice_channels:
            try:
                await archive_channel(
                    channel=channel,
                    conn=conn,
                    session=session,
                    downloads_folder=downloads_folder,
                    channel_type="voice",
                )
            except discord.Forbidden:
                print(f"  [SKIP] No access to VOICE {channel.name}")
            except Exception as e:
                print(f"  [ERROR] VOICE {channel.name}: {e}")

    conn.close()
    print(f"\n[DONE] Archive complete for {target_guild.name}.")
    print(f"[DONE] Database saved to: {db_path}")
    print(f"[DONE] Attachments saved to: {downloads_folder}")
    await client.close()


if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is missing. Set it in Discord_scrape/.env and run again."
    )

client.run(TOKEN)
