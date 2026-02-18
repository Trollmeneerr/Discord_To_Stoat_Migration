import discord
import asyncio
import sqlite3
import aiohttp
import os
import re
from datetime import datetime

# Load token from environment variable
TOKEN = os.getenv("DISCORD_TOKEN")

DISCORD_LINK_RE = re.compile(
    r"https://discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)"
)

# Setup intents - we need message content and guild access
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

client = discord.Client(intents=intents)


def init_db():
    """Create the SQLite database and tables if they don't exist."""
    conn = sqlite3.connect("discord_archive.db")
    c = conn.cursor()

    # Table for servers (guilds)
    c.execute("""
        CREATE TABLE IF NOT EXISTS guilds (
            id TEXT PRIMARY KEY,
            name TEXT
        )
    """)

    # Table for channels — type is either 'text' or 'voice'
    c.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id TEXT PRIMARY KEY,
            guild_id TEXT,
            name TEXT,
            type TEXT DEFAULT 'text',
            FOREIGN KEY (guild_id) REFERENCES guilds(id)
        )
    """)

    # Table for users
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT,
            display_name TEXT
        )
    """)

    # Table for messages
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            channel_id TEXT,
            guild_id TEXT,
            author_id TEXT,
            content TEXT,
            timestamp TEXT,
            has_attachments INTEGER DEFAULT 0,
            FOREIGN KEY (channel_id) REFERENCES channels(id),
            FOREIGN KEY (author_id) REFERENCES users(id)
        )
    """)

    # Table for attachments (images, videos, files)
    c.execute("""
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
    """)

    # Table for message link redirects
    # Stores every Discord message link found inside any message content.
    # The importer will use this to rewrite them to Stoat links after import.
    c.execute("""
        CREATE TABLE IF NOT EXISTS redirects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_message_id TEXT,      -- Discord ID of the message that CONTAINS the link
            linked_guild_id TEXT,        -- Guild ID from the Discord link
            linked_channel_id TEXT,      -- Channel ID from the Discord link
            linked_message_id TEXT,      -- Message ID from the Discord link (the target)
            original_url TEXT            -- The full original Discord URL
        )
    """)

    conn.commit()
    conn.close()
    print("[DB] Database initialized.")


async def download_attachment(session, url, filename, folder="downloads"):
    """Download an attachment and save it locally."""
    os.makedirs(folder, exist_ok=True)
    filepath = os.path.join(folder, filename)

    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                with open(filepath, "wb") as f:
                    f.write(await resp.read())
                return filepath
    except Exception as e:
        print(f"[WARN] Failed to download {filename}: {e}")
    return None


def extract_redirects(message_id, content):
    """
    Scan message content for Discord message links.
    Returns a list of redirect rows ready to insert into the DB.
    """
    redirects = []
    if not content:
        return redirects
    for match in DISCORD_LINK_RE.finditer(content):
        guild_id, channel_id, linked_msg_id = match.groups()
        redirects.append((
            message_id,
            guild_id,
            channel_id,
            linked_msg_id,
            match.group(0)  # full original URL
        ))
    return redirects


async def archive_channel(channel, conn, session, channel_type="text"):
    """
    Fetch all messages from a text or voice channel and save to DB.
    Also detects and saves any Discord message links found in content.
    """
    c = conn.cursor()

    # Save channel to DB with its type
    c.execute("INSERT OR IGNORE INTO channels VALUES (?, ?, ?, ?)",
              (str(channel.id), str(channel.guild.id), channel.name, channel_type))
    conn.commit()

    label = "🔊" if channel_type == "voice" else "#"
    print(f"  [→] Archiving {label}{channel.name} ({channel_type})...")
    count = 0
    redirect_count = 0

    try:
        async for message in channel.history(limit=None, oldest_first=True):

            # Save user
            c.execute("INSERT OR IGNORE INTO users VALUES (?, ?, ?)",
                      (str(message.author.id),
                       str(message.author.name),
                       str(message.author.display_name)))

            has_attachments = 1 if message.attachments else 0

            # Save message
            c.execute("INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (str(message.id),
                       str(channel.id),
                       str(channel.guild.id),
                       str(message.author.id),
                       message.content,
                       message.created_at.isoformat(),
                       has_attachments))

            # Detect and save any Discord message links in the content
            redirects = extract_redirects(str(message.id), message.content)
            for r in redirects:
                c.execute(
                    "INSERT INTO redirects (source_message_id, linked_guild_id, linked_channel_id, linked_message_id, original_url) VALUES (?, ?, ?, ?, ?)",
                    r
                )
                redirect_count += 1

            # Save and download attachments (videos, images, files)
            for attachment in message.attachments:
                local_path = await download_attachment(
                    session, attachment.url, f"{attachment.id}_{attachment.filename}"
                )
                c.execute("INSERT OR IGNORE INTO attachments VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (str(attachment.id),
                           str(message.id),
                           attachment.filename,
                           attachment.url,
                           attachment.content_type or "unknown",
                           attachment.size,
                           local_path))

            count += 1
            if count % 500 == 0:
                conn.commit()
                print(f"    [~] {count} messages saved...")

    except discord.errors.HTTPException as e:
        # Voice channels with no text history return 400 — that's fine
        if e.status == 400:
            print(f"  [INFO] {label}{channel.name} has no text chat history, skipping messages.")
        else:
            raise

    conn.commit()
    print(f"  [✓] {label}{channel.name} done — {count} messages, {redirect_count} links detected.")


@client.event
async def on_ready():
    print(f"[BOT] Logged in as {client.user}")
    print("[BOT] Starting archive process...\n")

    init_db()
    conn = sqlite3.connect("discord_archive.db")

    async with aiohttp.ClientSession() as session:
        for guild in client.guilds:
            print(f"[GUILD] Archiving: {guild.name} ({guild.id})")

            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO guilds VALUES (?, ?)",
                      (str(guild.id), guild.name))
            conn.commit()

            # Archive all text channels
            print(f"\n[TEXT CHANNELS]")
            for channel in guild.text_channels:
                try:
                    await archive_channel(channel, conn, session, channel_type="text")
                except discord.Forbidden:
                    print(f"  [SKIP] No access to #{channel.name}")
                except Exception as e:
                    print(f"  [ERROR] #{channel.name}: {e}")

            # Archive all voice channels (structure + in-VC text chat)
            print(f"\n[VOICE CHANNELS]")
            for channel in guild.voice_channels:
                try:
                    await archive_channel(channel, conn, session, channel_type="voice")
                except discord.Forbidden:
                    print(f"  [SKIP] No access to 🔊{channel.name}")
                except Exception as e:
                    print(f"  [ERROR] 🔊{channel.name}: {e}")

    conn.close()
    print("\n[DONE] Archive complete! Database saved to discord_archive.db")
    await client.close()


# Run the bot
client.run(TOKEN)