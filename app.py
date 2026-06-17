"""
app.py — Task Manager PWA Backend (FastAPI)
-------------------------------------------
Run:
  uvicorn app:app --host 0.0.0.0 --port 8000 --reload

Or with HTTPS (needed for push notifications on real devices):
  uvicorn app:app --host 0.0.0.0 --port 8000 --ssl-keyfile key.pem --ssl-certfile cert.pem
"""

import json
import os
import re
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import database as db

# ── Firebase push notifications (uses firebase-admin SDK) ──────────────────
try:
    import firebase_admin
    from firebase_admin import credentials, messaging as fb_messaging
    _fb_cred_path = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "firebase-service-account.json")
    if os.path.exists(_fb_cred_path):
        _cred = credentials.Certificate(_fb_cred_path)
        firebase_admin.initialize_app(_cred)
        PUSH_ENABLED = True
    else:
        PUSH_ENABLED = False
except Exception:
    PUSH_ENABLED = False

# FCM device tokens — stored in memory (persists until restart)
# For production, store in SQLite instead
fcm_tokens: list[str] = []

# ── App setup ──────────────────────────────────────────────────────────────

app = FastAPI(title="Task Manager")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# In-memory push subscription store (keyed by endpoint)
push_subscriptions: dict[str, dict] = {}

# ── Default user ID (single-user app) ─────────────────────────────────────
USER_ID = 1

# ── Pydantic models ────────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    text: str

class TaskIn(BaseModel):
    title: str
    category: str = "general"
    priority: int = 2
    due_at: str | None = None

class ReminderIn(BaseModel):
    task_id: int
    remind_at: str  # ISO datetime string

class PushSubscription(BaseModel):
    endpoint: str
    keys: dict

# ── Natural language parser ────────────────────────────────────────────────

IT_KEYWORDS = {
    "server", "ticket", "deploy", "deployment", "reboot", "backup", "patch",
    "firewall", "vpn", "dns", "ssl", "certificate", "monitor", "alert",
    "incident", "outage", "switch", "router", "vm", "virtual", "script",
    "cron", "log", "logs", "update", "upgrade", "network", "ssh", "rdp",
    "active directory", "ad", "azure", "aws", "linux", "windows", "disk",
    "storage", "database", "sql", "api", "port", "security", "printer",
}

PRIORITY_WORDS = {
    "urgent": 1, "critical": 1, "asap": 1, "high": 1, "important": 1,
    "low": 3, "whenever": 3, "eventually": 3,
}


def detect_category(text: str) -> str:
    words = set(text.lower().split())
    return "it" if words & IT_KEYWORDS else "general"


def detect_priority(text: str) -> int:
    lower = text.lower()
    for word, pri in PRIORITY_WORDS.items():
        if word in lower:
            return pri
    return 2


def parse_time_phrase(text: str) -> datetime | None:
    now = datetime.now()
    text = text.strip().lower()

    m = re.search(r"in (\d+)\s*(minute|minutes|min|hour|hours|h)\b", text)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        return now + (timedelta(hours=n) if "h" in unit else timedelta(minutes=n))

    m = re.search(r"\bat (\d{1,2}):(\d{2})\b", text)
    if m:
        t = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        return t if t > now else t + timedelta(days=1)

    m = re.search(r"\bat (\d{1,2})(am|pm)\b", text)
    if m:
        h = int(m.group(1))
        if m.group(2) == "pm" and h != 12:
            h += 12
        elif m.group(2) == "am" and h == 12:
            h = 0
        t = now.replace(hour=h, minute=0, second=0, microsecond=0)
        return t if t > now else t + timedelta(days=1)

    if "tomorrow" in text:
        return (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)

    return None


def parse_chat(text: str) -> dict:
    """
    Turn a natural language message into a structured intent.
    Returns: {"intent": str, ...payload}
    """
    t = text.strip()
    lower = t.lower()

    # Done / complete
    m = re.match(r"^(?:done|complete|finished|tick off|mark done)\s+#?(\d+)", lower)
    if m:
        return {"intent": "done", "task_id": int(m.group(1))}

    if re.match(r"^(?:done|complete|finished)$", lower):
        return {"intent": "prompt_done"}

    # Delete
    m = re.match(r"^(?:delete|remove|drop)\s+#?(\d+)", lower)
    if m:
        return {"intent": "delete", "task_id": int(m.group(1))}

    # List
    if re.match(r"^(?:list|show|tasks?|what do i have|what's on my list|show me my tasks?)$", lower):
        return {"intent": "list"}

    # Next / nudge
    if re.match(r"^(?:next|what(?:'s| is) next|what should i do|nudge|guide me|help)$", lower):
        return {"intent": "next"}

    # Reminder with "remind me"
    m = re.search(r"remind(?:er)? (?:me )?(?:about )?(.+?) at (.+)", lower)
    if m:
        task_text = m.group(1).strip()
        time_part = m.group(2).strip()
        remind_at = parse_time_phrase("at " + time_part)
        return {"intent": "add_with_reminder", "title": task_text, "remind_at": remind_at}

    # "remind me in 2 hours" (about most recent task)
    m = re.match(r"remind me (.+)", lower)
    if m:
        remind_at = parse_time_phrase(m.group(1))
        if remind_at:
            return {"intent": "remind_last", "remind_at": remind_at}

    # Priority change
    m = re.match(r"^(?:priority|set priority)\s+#?(\d+)\s+(?:to\s+)?(\d|high|medium|low|urgent|critical)", lower)
    if m:
        tid = int(m.group(1))
        p = m.group(2)
        pri = {"high": 1, "urgent": 1, "critical": 1, "medium": 2, "low": 3}.get(p, int(p) if p.isdigit() else 2)
        return {"intent": "priority", "task_id": tid, "priority": pri}

    # Default: add task
    title = re.sub(r"^(?:add|create|new task|todo|to do|note)\s+", "", t, flags=re.I).strip()
    category = detect_category(title)
    priority = detect_priority(title)
    remind_at = parse_time_phrase(title)
    return {
        "intent": "add",
        "title": title,
        "category": category,
        "priority": priority,
        "remind_at": remind_at,
    }


NUDGE_OPENERS = [
    "Let's tackle this next:",
    "Here's what needs your attention:",
    "Up next for you:",
    "Time to focus on this one:",
]

import random

def build_nudge(task) -> str:
    opener = random.choice(NUDGE_OPENERS)
    lines = [opener, f"\n**[{task['id']}] {task['title']}**"]

    cat = task["category"]
    pri = task["priority"]
    due = task["due_at"]

    if cat == "it":
        lines.append("💻 IT task — clear your distractions and open your tools.")
    else:
        lines.append("👤 Personal — take a breath, then get it done.")

    if due:
        due_dt = datetime.fromisoformat(due)
        diff = due_dt - datetime.now()
        if diff.total_seconds() < 0:
            lines.append(f"⚠️ **OVERDUE** by {abs(int(diff.total_seconds()//60))} min!")
        elif diff.total_seconds() < 3600:
            lines.append(f"⏰ Due in {int(diff.total_seconds()//60)} minutes!")
        else:
            lines.append(f"⏰ Due: {due_dt.strftime('%a %d %b %H:%M')}")

    label = {1: "🔴 High", 2: "🟡 Medium", 3: "🟢 Low"}.get(pri, "")
    lines.append(f"Priority: {label}")
    lines.append(f"\nWhen done, say: **done {task['id']}**")
    return "\n".join(lines)


# ── Chat endpoint ──────────────────────────────────────────────────────────

@app.post("/chat")
async def chat(msg: ChatMessage):
    intent = parse_chat(msg.text)
    action = intent["intent"]
    last_task_id = None

    if action == "add":
        tid = db.add_task(USER_ID, intent["title"], intent["category"], intent["priority"])
        last_task_id = tid
        cat_label = "💻 IT task" if intent["category"] == "it" else "📋 General"
        reply = f"✅ Added **[{tid}] {intent['title']}**\n{cat_label} · Priority: {['','🔴 High','🟡 Medium','🟢 Low'][intent['priority']]}"

        if intent.get("remind_at"):
            db.add_reminder(USER_ID, tid, intent["remind_at"])
            reply += f"\n⏰ Reminder set for {intent['remind_at'].strftime('%a %d %b %H:%M')}"
        else:
            reply += f"\n\nSay **remind me at [time]** to set a reminder."

    elif action == "add_with_reminder":
        cat = detect_category(intent["title"])
        tid = db.add_task(USER_ID, intent["title"], cat)
        last_task_id = tid
        reply = f"✅ Added **[{tid}] {intent['title']}**"
        if intent.get("remind_at"):
            db.add_reminder(USER_ID, tid, intent["remind_at"])
            reply += f"\n⏰ Reminder: {intent['remind_at'].strftime('%a %d %b %H:%M')}"

    elif action == "remind_last":
        tasks = db.list_tasks(USER_ID)
        if not tasks:
            reply = "No tasks to remind you about yet."
        else:
            last = tasks[-1]
            db.add_reminder(USER_ID, last["id"], intent["remind_at"])
            reply = f"⏰ Reminder set for **[{last['id']}] {last['title']}** at {intent['remind_at'].strftime('%H:%M')}"

    elif action == "list":
        tasks = db.list_tasks(USER_ID)
        if not tasks:
            reply = "🎉 No pending tasks! You're all caught up."
        else:
            pri_icon = {1: "🔴", 2: "🟡", 3: "🟢"}
            cat_icon = {"it": "💻", "general": "📋", "personal": "👤"}
            lines = [f"**{len(tasks)} pending tasks:**\n"]
            for t in tasks:
                due = f" · ⏰ {t['due_at'][:16]}" if t["due_at"] else ""
                lines.append(f"{pri_icon.get(t['priority'],'⚪')} {cat_icon.get(t['category'],'📋')} [{t['id']}] {t['title']}{due}")
            lines.append("\nSay **next** for what to do first.")
            reply = "\n".join(lines)

    elif action == "next":
        task = db.get_next_task(USER_ID)
        if not task:
            reply = "🎉 All done! No pending tasks."
        else:
            reply = build_nudge(task)

    elif action == "done":
        ok = db.mark_done(USER_ID, intent["task_id"])
        if ok:
            reply = f"✅ Task [{intent['task_id']}] complete! Nice work."
            nxt = db.get_next_task(USER_ID)
            if nxt:
                reply += f"\n\n➡️ Next up: **[{nxt['id']}] {nxt['title']}**\nSay **next** for a full nudge."
        else:
            reply = f"No task [{intent['task_id']}] found."

    elif action == "prompt_done":
        reply = "Which task? Say **done [number]** — e.g. `done 3`"

    elif action == "delete":
        ok = db.delete_task(USER_ID, intent["task_id"])
        reply = f"🗑️ Task [{intent['task_id']}] deleted." if ok else f"No task [{intent['task_id']}] found."

    elif action == "priority":
        ok = db.set_priority(USER_ID, intent["task_id"], intent["priority"])
        label = {1: "🔴 High", 2: "🟡 Medium", 3: "🟢 Low"}.get(intent["priority"], "")
        reply = f"Task [{intent['task_id']}] set to {label}." if ok else f"No task [{intent['task_id']}] found."

    else:
        reply = "I didn't quite catch that. Try: **add [task]**, **list**, **next**, **done [id]**, or **remind me at [time]**."

    return {"reply": reply, "last_task_id": last_task_id}


# ── REST Task endpoints ────────────────────────────────────────────────────

@app.get("/tasks")
def get_tasks():
    return [dict(t) for t in db.list_tasks(USER_ID)]


@app.post("/tasks")
def create_task(task: TaskIn):
    tid = db.add_task(USER_ID, task.title, task.category, task.priority, task.due_at)
    return {"id": tid}


@app.post("/tasks/{task_id}/done")
def complete_task(task_id: int):
    if not db.mark_done(USER_ID, task_id):
        raise HTTPException(404, "Task not found")
    return {"ok": True}


@app.delete("/tasks/{task_id}")
def remove_task(task_id: int):
    if not db.delete_task(USER_ID, task_id):
        raise HTTPException(404, "Task not found")
    return {"ok": True}


@app.get("/tasks/next")
def next_task():
    task = db.get_next_task(USER_ID)
    return dict(task) if task else {}


# ── Push notification endpoints ────────────────────────────────────────────

class FcmSubscription(BaseModel):
    token: str


@app.post("/push/fcm-subscribe")
def fcm_subscribe(sub: FcmSubscription):
    """Register an Android FCM device token."""
    if sub.token and sub.token not in fcm_tokens:
        fcm_tokens.append(sub.token)
    return {"ok": True}


@app.post("/push/test")
def test_push():
    _send_push_all("Task Manager", "Push notifications are working! 🎉")
    return {"ok": True}


def _send_push_all(title: str, body: str, data: dict | None = None):
    """Send a push notification to all registered Android devices via FCM."""
    if not PUSH_ENABLED or not fcm_tokens:
        return
    dead = []
    for token in fcm_tokens:
        try:
            msg = fb_messaging.Message(
                notification=fb_messaging.Notification(title=title, body=body),
                data={k: str(v) for k, v in (data or {}).items()},
                android=fb_messaging.AndroidConfig(
                    priority="high",
                    notification=fb_messaging.AndroidNotification(
                        sound="default",
                        click_action="FLUTTER_NOTIFICATION_CLICK",
                    ),
                ),
                token=token,
            )
            fb_messaging.send(msg)
        except Exception as e:
            if "registration-token-not-registered" in str(e):
                dead.append(token)
    for d in dead:
        fcm_tokens.remove(d)


# ── Reminder scheduler ─────────────────────────────────────────────────────

async def reminder_loop():
    while True:
        await asyncio.sleep(30)
        due = db.get_due_reminders()
        for r in due:
            if r["done"]:
                db.mark_reminder_fired(r["id"])
                continue
            _send_push_all(
                "⏰ Task Reminder",
                r["title"],
                {"task_id": str(r["task_id"])},
            )
            db.mark_reminder_fired(r["id"])


@app.on_event("startup")
async def startup():
    db.init_db()
    asyncio.create_task(reminder_loop())


# ── Serve PWA ──────────────────────────────────────────────────────────────

# ── Knowledge Base endpoints ───────────────────────────────────────────────

class ArticleIn(BaseModel):
    title: str
    content: str
    tags: str = ""


class ImageIn(BaseModel):
    filename: str
    data: str  # base64 data URL, e.g. "data:image/png;base64,..."


@app.get("/kb")
def kb_list_articles(q: str = ""):
    rows = db.kb_search(q) if q else db.kb_list()
    return [dict(r) for r in rows]


@app.get("/kb/{article_id}")
def kb_get_article(article_id: int):
    article = db.kb_get(article_id)
    if not article:
        raise HTTPException(404, "Article not found")
    images = db.kb_get_images(article_id)
    return {**dict(article), "images": [dict(i) for i in images]}


@app.post("/kb")
def kb_create_article(article: ArticleIn):
    aid = db.kb_create(article.title, article.content, article.tags)
    return {"id": aid}


@app.put("/kb/{article_id}")
def kb_update_article(article_id: int, article: ArticleIn):
    if not db.kb_update(article_id, article.title, article.content, article.tags):
        raise HTTPException(404, "Article not found")
    return {"ok": True}


@app.delete("/kb/{article_id}")
def kb_delete_article(article_id: int):
    if not db.kb_delete(article_id):
        raise HTTPException(404, "Article not found")
    return {"ok": True}


@app.post("/kb/{article_id}/images")
def kb_upload_image(article_id: int, image: ImageIn):
    iid = db.kb_add_image(article_id, image.filename, image.data)
    return {"id": iid}


@app.delete("/kb/images/{image_id}")
def kb_delete_image(image_id: int):
    if not db.kb_delete_image(image_id):
        raise HTTPException(404, "Image not found")
    return {"ok": True}


# ── Serve PWA ──────────────────────────────────────────────────────────────

@app.get("/")
def serve_app():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/sw.js")
def serve_sw():
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")


# ── SMS via Twilio ─────────────────────────────────────────────────────────
# Twilio sends a POST to /sms when you receive a text.
# Set your Twilio number's webhook to: https://YOUR_SERVER/sms
#
# Install: pip install twilio
# Set env vars: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER
# Your phone number (only you can text in): ALLOWED_PHONE=+1xxxxxxxxxx

from fastapi import Request
from fastapi.responses import PlainTextResponse
from urllib.parse import parse_qs

ALLOWED_PHONE = os.environ.get("ALLOWED_PHONE", "")  # e.g. "+14155551234"


@app.post("/sms", response_class=PlainTextResponse)
async def sms_webhook(request: Request):
    raw = await request.body()
    params = parse_qs(raw.decode())
    From = params.get("From", [""])[0]
    Body = params.get("Body", [""])[0]

    if ALLOWED_PHONE and From != ALLOWED_PHONE:
        return "<?xml version='1.0'?><Response></Response>"

    text = Body.strip()
    intent = parse_chat(text)
    action = intent["intent"]

    if action == "add":
        tid = db.add_task(USER_ID, intent["title"], intent["category"], intent["priority"])
        cat = "IT" if intent["category"] == "it" else "General"
        reply = f"Added [{tid}] {intent['title']} ({cat})"
        if intent.get("remind_at"):
            db.add_reminder(USER_ID, tid, intent["remind_at"])
            reply += f"\nReminder: {intent['remind_at'].strftime('%a %d %b %H:%M')}"

    elif action == "list":
        tasks = db.list_tasks(USER_ID)
        if not tasks:
            reply = "No pending tasks!"
        else:
            pri = {1:"[!]", 2:"[~]", 3:"[ ]"}
            lines = [f"{len(tasks)} tasks:"]
            for t in tasks:
                lines.append(f"{pri.get(t['priority'],'[ ]')} [{t['id']}] {t['title']}")
            reply = "\n".join(lines)

    elif action == "next":
        task = db.get_next_task(USER_ID)
        if not task:
            reply = "All done! No pending tasks."
        else:
            due = f"\nDue: {task['due_at'][:16]}" if task["due_at"] else ""
            reply = f"Next up:\n[{task['id']}] {task['title']}{due}\n\nReply: done {task['id']}"

    elif action == "done":
        ok = db.mark_done(USER_ID, intent["task_id"])
        reply = f"Done! Task [{intent['task_id']}] complete." if ok else f"Task [{intent['task_id']}] not found."

    elif action == "delete":
        ok = db.delete_task(USER_ID, intent["task_id"])
        reply = f"Deleted [{intent['task_id']}]." if ok else f"Task [{intent['task_id']}] not found."

    elif action == "remind_last":
        tasks = db.list_tasks(USER_ID)
        if tasks:
            last = tasks[-1]
            db.add_reminder(USER_ID, last["id"], intent["remind_at"])
            reply = f"Reminder set for [{last['id']}] {last['title']} at {intent['remind_at'].strftime('%H:%M')}"
        else:
            reply = "No tasks yet."

    else:
        reply = "Commands: list, next, done [id], delete [id]\nOr just type a task to add it."

    # TwiML response
    twiml = f"<?xml version='1.0'?><Response><Message>{reply}</Message></Response>"
    return PlainTextResponse(twiml, media_type="application/xml")
