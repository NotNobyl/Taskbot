# TaskBot — Setup Guide

## What You're Building

A personal task manager with three ways to interact:

| Interface | Use from |
|---|---|
| 📱 PWA (web app) | Phone browser or PC — installs like a real app |
| 💬 Discord DM | Discord on phone or PC desktop |
| 📲 SMS | Text your phone number |

All three feed into the same task database. Any task added via SMS shows up in the app and Discord, and vice versa.

---

## Step 1 — Install Python dependencies

Requires Python 3.11+

```bash
pip install -r requirements.txt
```

---

## Step 2 — Run the web app

```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000` in your browser. On Android, open Chrome, go to that address (use your PC's LAN IP if on the same network), then tap the browser menu → **Add to Home Screen**.

---

## Step 3 — Discord bot (optional but recommended)

This lets you DM your bot from Discord on phone or PC.

**Create the bot:**
1. Go to https://discord.com/developers/applications → New Application
2. Go to Bot → Add Bot → copy the Token
3. Under Privileged Gateway Intents → enable **Message Content Intent**
4. Go to OAuth2 → URL Generator → select `bot` scope + `Send Messages` + `Read Message History`
5. Open the generated URL to invite the bot to a server, OR enable DMs directly

**Get your Discord user ID:**
Discord → Settings → Advanced → enable Developer Mode → right-click your username → Copy User ID

**Run the Discord bot:**
```bash
DISCORD_TOKEN="your_bot_token" DISCORD_USER_ID="your_user_id" python discord_bot.py
```

Now DM the bot on Discord from your phone or PC. Just type naturally:

```
patch the switches tonight
what's next
done 3
remind me at 4pm to check backup logs
```

---

## Step 4 — SMS via Twilio (optional)

This lets you text a phone number to add tasks.

**Set up Twilio:**
1. Sign up at https://www.twilio.com (free trial gives you credits)
2. Buy a phone number (~$1/month after trial)
3. Go to Phone Numbers → your number → Messaging → set Webhook to:
   `https://YOUR_SERVER/sms` (POST)

**Expose your server to the internet** (if running locally):
```bash
# Use ngrok (free) for testing:
ngrok http 8000
# Copy the https URL, paste it as your Twilio webhook
```

**Set env vars and restart:**
```bash
TWILIO_ACCOUNT_SID="ACxxx" \
TWILIO_AUTH_TOKEN="xxx" \
ALLOWED_PHONE="+14155551234" \  # your personal phone number
uvicorn app:app --host 0.0.0.0 --port 8000
```

Now text your Twilio number from your phone — plain English works:
```
reboot the backup server
list
next
done 2
```

---

## Step 5 — Push notifications in the app

For reminder push notifications in the PWA, you need HTTPS and VAPID keys.

**Generate VAPID keys:**
```bash
python -c "from py_vapid import Vapid; v=Vapid(); v.generate_keys(); print('Private:', v.private_key.private_bytes_raw().hex()); print('Public:', v.public_key.public_bytes_raw().hex())"
```

Or use: https://web-push-codelab.glitch.me/

**Set env vars:**
```bash
VAPID_PRIVATE_KEY="your_private_key"
VAPID_PUBLIC_KEY="your_public_key"
VAPID_EMAIL="mailto:you@example.com"
```

Then in the app, tap 🔔 to enable notifications.

---

## Step 6 — Keep it running 24/7

```ini
# /etc/systemd/system/taskbot.service
[Unit]
Description=TaskBot Web App
After=network.target

[Service]
WorkingDirectory=/path/to/taskbot
ExecStart=/usr/bin/uvicorn app:app --host 0.0.0.0 --port 8000
Environment=BOT_TOKEN=...
Environment=DISCORD_TOKEN=...
Environment=ALLOWED_PHONE=+1...
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
# Run Discord bot separately:
# /etc/systemd/system/taskbot-discord.service
ExecStart=/usr/bin/python3 discord_bot.py
```

---

## Natural language reference

Just type anything — no commands required:

| What you type | What happens |
|---|---|
| `patch the switches tonight` | Adds IT task (auto-detected) |
| `call dentist tomorrow` | Adds personal task with tomorrow nudge |
| `urgent: fix the VPN` | Adds high-priority IT task |
| `list` | Shows all pending tasks |
| `next` | Nudges you on the highest priority task |
| `done 3` | Marks task #3 complete |
| `remind me at 3pm` | Reminds you about the last added task |
| `remind me about the VPN at 14:30` | Adds task + reminder together |
| `remind me in 2 hours` | Reminder in 2 hours |
| `delete 4` | Removes task #4 |
| `priority 2 1` | Sets task #2 to high priority |

---

## File structure

```
taskbot/
├── app.py              — FastAPI backend + SMS webhook
├── discord_bot.py      — Discord DM bot
├── database.py         — SQLite task/reminder storage
├── requirements.txt
├── tasks.db            — created automatically
├── static/
│   ├── index.html      — PWA chat interface
│   ├── sw.js           — service worker (push notifications)
│   └── manifest.json   — PWA manifest
└── SETUP.md
```
