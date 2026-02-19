# Discord -> Stoat Migration Toolkit

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Status](https://img.shields.io/badge/Status-Production--Ready-success)
![Database](https://img.shields.io/badge/Database-SQLite-lightgrey)

Archive a Discord server into SQLite, then import it into Stoat.

## Notes

- Bot must be able to read all Discord channels you want archived.
- Private channels require manual bot access.
- Deleted Discord messages cannot be recovered.
- Large servers may take hours to days.
- Keep enough disk space for attachments.

## Repository Structure

```text
Discord_To_Stoat_Migration/
|-- Discord_scrape/
|   |-- bot.py
|   |-- validate.py
|   |-- .env.example
|
|-- Stoat_migration/
|   |-- importer.py
|   |-- .env.example
|
|-- Discord/downloads/
|   |-- Attachments.ext
|
|-- requirements.txt
|-- README.md
```

## Manual Setup 

### 1. Create a Discord Bot

1. Go to: [https://discord.com/developers/applications](https://discord.com/developers/applications)
2. Create **New Application → Bot_Name**
3. Go to Bot
4. Copy the bot token
5. Enable **Privileged Gateway Intents**:

   * ✅ Server Members Intent
   * ✅ Message Content Intent

6. Go to **OAuth2**

7. Generate bot link with:
   * Redirects -> http://localhost:5328/callback (Use a port that is not used)
   * Scopes -> **bot**
   * Permission -> **Read Message History**

8. Copy the generated URL and paste it into your browser
---

### 2. Configure Stoat

1. Log in at `https://stoat.chat`
2. Go to Settings -> Bots -> Create Bot.
3. Copy the Stoat bot token.
4. Invite the bot to your Stoat server.
5. Ensure bot permissions include:
- `Manage Channels`
- `Send Messages`
6. Copy your Stoat Server ID from right clicking server icon. (You might need to enable this feature is advanced setting in your profile) 

### 3. Configure the project

1. Install Python dependencies:

```bash
pip install -r requirements.txt
```

2. Create env files:

```bash
cp Discord_scrape/.env.example Discord_scrape/.env
cp Stoat_migration/.env.example Stoat_migration/.env
```

3. Fill env values:

```env
# Discord_scrape/.env
DISCORD_TOKEN=your_discord_bot_token
DISCORD_MESSAGE_LIMIT=none
```

`DISCORD_MESSAGE_LIMIT` options:
- `none` (or empty): archive full history
- positive integer (for example `100`): archive latest N messages per channel

```env
# Stoat_migration/.env
STOAT_TOKEN=your_stoat_bot_token
STOAT_SERVER_ID=your_stoat_server_id
```

4. Run the Discord scraper:

```bash
cd Discord_scrape
python bot.py
```

5. Optional: inspect archived data:

```bash
python validate.py
```

6. Run the Stoat importer:

```bash
cd ../Stoat_migration
python importer.py
```

## Output

After scraping:
- `Discord_scrape/discord_archive.db`
- `Discord_scrape/downloads/`

## What Gets Migrated

| Feature | Supported |
| --- | --- |
| Channels | Yes |
| Messages | Yes |
| Attachments <= 20MB | Uploaded |
| Attachments > 20MB | Link posted |
| Author attribution | Yes |
| Long messages | Trimmed to 2000 chars |
| Rate limits | Delay buffer in importer |

## Database Schema

| Table | Description |
| --- | --- |
| guilds | Server metadata |
| channels | All channels |
| users | Message authors |
| messages | Message content |
| attachments | File metadata |
| redirects | Discord link rewrite mapping |

## Requirements

- Python 3.9+
- SQLite (bundled with Python)

## License

MIT License — feel free to modify and use.

---

## Author
Trollmeneerr

--Contains AI Generated content--