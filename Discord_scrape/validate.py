import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def resolve_db_path():
    """
    Resolve DB path in this order:
    1) first CLI argument (python validate.py <path>)
    2) legacy Discord_scrape/discord_archive.db
    3) newest Discord_scrape/archives/*/discord_archive.db
    """
    if len(sys.argv) > 1:
        candidate = Path(sys.argv[1]).expanduser()
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        if candidate.exists():
            return candidate
        raise SystemExit(f"DB file not found: {candidate}")

    legacy = BASE_DIR / "discord_archive.db"
    if legacy.exists():
        return legacy

    archives_root = BASE_DIR / "archives"
    if archives_root.exists():
        candidates = sorted(
            archives_root.glob("*/discord_archive.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]

    raise SystemExit(
        "No archive DB found. Run bot.py first, or pass a DB path:\n"
        "python validate.py <path_to_discord_archive.db>"
    )


db_path = resolve_db_path()
print(f"[INFO] Using DB: {db_path}")

conn = sqlite3.connect(str(db_path))
c = conn.cursor()

total = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
print(f"=== TOTAL MESSAGES: {total} ===\n")

print("=== TEXT CHANNELS ===")
text_channels = c.execute(
    """
    SELECT channels.name, COUNT(messages.id) AS msg_count
    FROM channels
    LEFT JOIN messages ON messages.channel_id = channels.id
    WHERE channels.type = 'text'
    GROUP BY channels.id
    ORDER BY msg_count DESC
"""
).fetchall()

for name, count in text_channels:
    print(f"  #{name}: {count} messages")

print(f"  Total: {len(text_channels)} text channels\n")

print("=== VOICE CHANNELS ===")
voice_channels = c.execute(
    """
    SELECT channels.name, COUNT(messages.id) AS msg_count
    FROM channels
    LEFT JOIN messages ON messages.channel_id = channels.id
    WHERE channels.type = 'voice'
    GROUP BY channels.id
    ORDER BY msg_count DESC
"""
).fetchall()

for name, count in voice_channels:
    print(f"  VOICE {name}: {count} messages")

print(f"  Total: {len(voice_channels)} voice channels\n")

print("=== MESSAGES PER USER ===")
for username, count in c.execute(
    """
    SELECT users.username, COUNT(messages.id) AS msg_count
    FROM messages
    JOIN users ON messages.author_id = users.id
    GROUP BY users.id
    ORDER BY msg_count DESC
"""
):
    print(f"  {username}: {count} messages")

print("\n=== ATTACHMENTS BY TYPE ===")
for content_type, count in c.execute(
    """
    SELECT content_type, COUNT(*)
    FROM attachments
    GROUP BY content_type
    ORDER BY COUNT(*) DESC
"""
):
    print(f"  {content_type}: {count}")

conn.close()
