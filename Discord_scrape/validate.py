import sqlite3

conn = sqlite3.connect("discord_archive.db")
c = conn.cursor()

# ── Total messages ────────────────────────────────────────────────────────
total = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
print(f"=== TOTAL MESSAGES: {total} ===\n")

# ── Text channels with message count ─────────────────────────────────────
print("=== TEXT CHANNELS ===")
text_channels = c.execute("""
    SELECT channels.name, COUNT(messages.id) as msg_count
    FROM channels
    LEFT JOIN messages ON messages.channel_id = channels.id
    WHERE channels.type = 'text'
    GROUP BY channels.id
    ORDER BY msg_count DESC
""").fetchall()

for name, count in text_channels:
    print(f"  #{name}: {count} messages")

print(f"  Total: {len(text_channels)} text channels\n")

# ── Voice channels with message count ────────────────────────────────────
print("=== VOICE CHANNELS ===")
voice_channels = c.execute("""
    SELECT channels.name, COUNT(messages.id) as msg_count
    FROM channels
    LEFT JOIN messages ON messages.channel_id = channels.id
    WHERE channels.type = 'voice'
    GROUP BY channels.id
    ORDER BY msg_count DESC
""").fetchall()

for name, count in voice_channels:
    print(f"  🔊{name}: {count} messages")

print(f"  Total: {len(voice_channels)} voice channels\n")

# ── Top 10 most active users ──────────────────────────────────────────────
print("=== MESSAGES PER USER ===")
for row in c.execute("""
    SELECT users.username, COUNT(messages.id) as msg_count
    FROM messages
    JOIN users ON messages.author_id = users.id
    GROUP BY users.id
    ORDER BY msg_count DESC
"""):
    print(f"  {row[0]}: {row[1]} messages")

# ── Attachments by type ───────────────────────────────────────────────────
print("\n=== ATTACHMENTS BY TYPE ===")
for row in c.execute("""
    SELECT content_type, COUNT(*) FROM attachments GROUP BY content_type ORDER BY COUNT(*) DESC
"""):
    print(f"  {row[0]}: {row[1]}")

conn.close()
