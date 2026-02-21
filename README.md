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
|   |-- archives/
|       |-- <server_name>_<server_id>/
|           |-- discord_archive.db
|           |-- downloads/
|
|-- Stoat_migration/
|   |-- importer.py
|   |-- .env.example
|
|-- requirements.txt
|-- setup.py
|-- automatic_setup_server.py
|-- automatic_setup_client.html
|-- README.md
```

## Prerequisites

- Python 3.14 recommended (3.9+ supported)
- pip (included with Python)
- SQLite (bundled with Python)

Install Python 3.14 from the official release page:
https://www.python.org/downloads/release/python-3140/

## Required Tokens and IDs

You need all of these before setup:
- Discord bot token (source server)
- Stoat bot token (target server)
- Stoat server ID (target server)

### Create Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications).
2. Create a new application and open the Bot section.
3. Copy the bot token.
4. Enable privileged intents:
- Server Members Intent
- Message Content Intent
5. In OAuth2, generate an invite link with:
- Redirect: `http://localhost:5000/redirect`
- Scope: `bot`
- Permission: `Read Message History`
6. Open the generated link and invite the bot to your Discord server.

### Create Stoat Bot

1. Log in at `https://stoat.chat`.
2. Go to Settings -> Bots -> Create Bot.
3. Copy the Stoat bot token.
4. Invite the bot to your Stoat server.
5. Ensure permissions include:
- `Manage Channels`
- `Send Messages`
6. Copy your Stoat server ID (developer mode may be required).

## Setup

Choose one setup method.

### Option A: Web Setup (Recommended)

1. Start the local web setup server:

```bash
python automatic_setup_server.py
```

2. Open in web browser:

```text
http://127.0.0.1:8080
```

3. In the page:
- Fill tokens and server ID
- Click `Save + Install Dependencies`
- Then run the scripts in the embedded terminal in order:
   1. Run Discord Scraper
   2. Run validate.py
   3. Run Stoat Importer

If youi are in more then 1 server you need to input a number according to the prompt.

The web page can:
- Save `Discord_scrape/.env` and `Stoat_migration/.env`
- Install dependencies
- Run `setup.py`, `Discord_scrape/bot.py`, `Discord_scrape/validate.py`, `Stoat_migration/importer.py`
- Send interactive input to running scripts

### Option B: Manual Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Create `.env` files:

```bash
cp Discord_scrape/.env.example Discord_scrape/.env
cp Stoat_migration/.env.example Stoat_migration/.env
```

PowerShell alternative:

```powershell
Copy-Item Discord_scrape/.env.example Discord_scrape/.env
Copy-Item Stoat_migration/.env.example Stoat_migration/.env
```

3. Fill Discord values in `Discord_scrape/.env`:

```env
DISCORD_TOKEN=your_discord_bot_token
DISCORD_MESSAGE_LIMIT=none
```

`DISCORD_MESSAGE_LIMIT` options:
- `none` (or empty): archive full history
- positive integer (for example `100`): archive latest N messages per channel

4. Fill Stoat values in `Stoat_migration/.env`:

```env
STOAT_TOKEN=your_stoat_bot_token
STOAT_SERVER_ID=your_stoat_server_id
```

Optional interactive CLI setup:

```bash
python setup.py
```

## Run the Migration

### 1. Run Discord Scraper

```bash
cd Discord_scrape
python bot.py
```

The bot will show all servers it is in and prompt you to choose one server.

Optional: inspect archive data

```bash
python validate.py
```

Output after scraping:
- `Discord_scrape/archives/<server_name>_<server_id>/discord_archive.db`
- `Discord_scrape/archives/<server_name>_<server_id>/downloads/`

### 2. Run Stoat Importer

```bash
cd Stoat_migration
python importer.py
```

The importer will ask you to choose the scraped archive database if multiple are found.

Make sure `STOAT_SERVER_ID` is correct before importing.

## What Gets Migrated

| Feature | Supported |
| --- | --- |
| Channels | Yes |
| Messages | Yes |
| Attachments <= 20MB | Uploaded |
| Attachments > 20MB | Link posted |
| Author attribution | Yes |
| Long messages | Trimmed to 2000 chars |
| Replies | Yes |
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

- Python 3.14 recommended (3.9+ supported)
- SQLite (bundled with Python)

## License

MIT License - feel free to modify and use.

## Author

Trollmeneerr

Contains AI-generated content.
