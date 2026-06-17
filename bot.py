"""
bot.py — Daily Task Manager Telegram Bot
-----------------------------------------
Commands:
  /add [task]          — add a task (or just type any message)
  /list                — show pending tasks
  /done [id]           — mark a task complete
  /delete [id]         — delete a task
  /next                — get a nudge on what to do next
  /priority [id] [1-3] — set priority (1=high, 2=medium, 3=low)
  /remind [id] [time]  — set a reminder (e.g. /remind 3 14:30 or /remind 3 "in 2 hours")
  /reminders           — list upcoming reminders
  /help                — show help

Run:
  pip install python-telegram-bot apscheduler
  BOT_TOKEN=<your_token> python bot.py
"""

import os
import logging
import re
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database as db

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN environment variable before running.")

# ── Helpers ────────────────────────────────────────────────────────────────

PRIORITY_EMOJI = {1: "🔴", 2: "🟡", 3: "🟢"}
CATEGORY_EMOJI = {"it": "💻", "personal": "👤", "general": "📋"}


def fmt_task(row) -> str:
    pri = PRIORITY_EMOJI.get(row["priority"], "⚪")
    cat = CATEGORY_EMOJI.get(row["category"], "📋")
    due = f"  ⏰ due {row['due_at'][:16]}" if row["due_at"] else ""
    return f"{pri} {cat} [{row['id']}] {row['title']}{due}"


def parse_time(text: str) -> datetime | None:
    """Parse a time string like '14:30', 'in 2 hours', 'in 30 minutes', or ISO datetime."""
    now = datetime.now()
    text = text.strip().lower()

    # "in X minutes/hours"
    m = re.match(r"in (\d+)\s*(minute|minutes|min|hour|hours|h)", text)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = timedelta(hours=n) if "h" in unit else timedelta(minutes=n)
        return now + delta

    # "HH:MM" (assume today, or tomorrow if already past)
    m = re.match(r"(\d{1,2}):(\d{2})$", text)
    if m:
        t = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        return t

    # ISO or partial date
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass

    return None


def nudge_message(task) -> str:
    """Build a friendly nudge for the next task."""
    now = datetime.now()
    lines = ["🎯 *Next up:*", f"`{task['title']}`"]

    if task["category"] == "it":
        lines.append("\n💻 IT task — block distractions and get your terminal ready.")
    elif task["category"] == "personal":
        lines.append("\n👤 Personal task — take a moment, then knock it out.")

    if task["due_at"]:
        due = datetime.fromisoformat(task["due_at"])
        diff = due - now
        if diff.total_seconds() < 0:
            lines.append(f"\n⚠️ *OVERDUE* by {abs(int(diff.total_seconds() // 60))} min!")
        elif diff.total_seconds() < 3600:
            lines.append(f"\n⏰ Due in {int(diff.total_seconds() // 60)} minutes — soon!")
        else:
            lines.append(f"\n⏰ Due: {due.strftime('%a %d %b %H:%M')}")

    pri = {1: "high — handle this first", 2: "medium", 3: "low"}.get(task["priority"], "")
    lines.append(f"\n📊 Priority: {pri}")
    lines.append("\nWhen done: `/done " + str(task["id"]) + "`")

    return "\n".join(lines)


# ── Command Handlers ───────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Daily Task Manager*\n\n"
        "Send me any message to add it as a task, or use:\n"
        "`/add [task]` — add a task\n"
        "`/list` — show tasks\n"
        "`/next` — what should I do next?\n"
        "`/done [id]` — complete a task\n"
        "`/remind [id] [time]` — set a reminder\n"
        "`/help` — full command list",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Commands:*\n"
        "`/add [task]` — add a task\n"
        "`/list` — pending tasks\n"
        "`/next` — nudge: what to do now\n"
        "`/done [id]` — mark complete\n"
        "`/delete [id]` — delete a task\n"
        "`/priority [id] [1-3]` — set priority (1=high)\n"
        "`/remind [id] [time]` — set reminder\n"
        "   e.g. `/remind 3 14:30` or `/remind 3 in 2 hours`\n"
        "`/reminders` — upcoming reminders\n\n"
        "*Categories auto-detected:* IT keywords → 💻, else 📋\n"
        "*Priorities:* 🔴 high  🟡 medium  🟢 low",
        parse_mode=ParseMode.MARKDOWN,
    )


IT_KEYWORDS = {
    "server", "ticket", "deploy", "deployment", "reboot", "backup", "patch",
    "firewall", "vpn", "dns", "ssl", "certificate", "monitor", "alert",
    "incident", "outage", "switch", "router", "vm", "virtual", "script",
    "cron", "log", "logs", "update", "upgrade", "network", "ssh", "rdp",
    "active directory", "ad", "azure", "aws", "linux", "windows", "disk",
    "storage", "database", "sql", "api", "port", "security",
}


def detect_category(title: str) -> str:
    words = set(title.lower().split())
    if words & IT_KEYWORDS:
        return "it"
    return "general"


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    title = " ".join(ctx.args).strip() if ctx.args else ""
    if not title:
        await update.message.reply_text("Usage: `/add [task title]`", parse_mode=ParseMode.MARKDOWN)
        return
    category = detect_category(title)
    tid = db.add_task(uid, title, category=category)
    cat_label = "💻 IT task" if category == "it" else "📋 General task"
    await update.message.reply_text(
        f"✅ Added [{tid}]: {title}\n{cat_label}\n\nUse `/priority {tid} 1` to mark as high priority.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def plain_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Treat any plain message as a task to add."""
    uid = update.effective_user.id
    title = update.message.text.strip()
    if not title:
        return
    category = detect_category(title)
    tid = db.add_task(uid, title, category=category)
    cat_label = "💻 IT task" if category == "it" else "📋 General task"
    await update.message.reply_text(
        f"✅ Added [{tid}]: {title}\n{cat_label}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    tasks = db.list_tasks(uid)
    if not tasks:
        await update.message.reply_text("No pending tasks! 🎉")
        return
    lines = ["*Pending tasks:*\n"]
    for t in tasks:
        lines.append(fmt_task(t))
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_next(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    task = db.get_next_task(uid)
    if not task:
        await update.message.reply_text("No pending tasks! 🎉 You're all caught up.")
        return
    await update.message.reply_text(nudge_message(task), parse_mode=ParseMode.MARKDOWN)


async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("Usage: `/done [task id]`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("Task ID must be a number.")
        return
    if db.mark_done(uid, tid):
        await update.message.reply_text(f"✅ Task [{tid}] marked complete! Nice work.")
        # Suggest next
        nxt = db.get_next_task(uid)
        if nxt:
            await update.message.reply_text(
                f"➡️ Up next: [{nxt['id']}] {nxt['title']}\n\n/next for a full nudge.",
                parse_mode=ParseMode.MARKDOWN,
            )
    else:
        await update.message.reply_text(f"No task [{tid}] found.")


async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("Usage: `/delete [task id]`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("Task ID must be a number.")
        return
    if db.delete_task(uid, tid):
        await update.message.reply_text(f"🗑️ Task [{tid}] deleted.")
    else:
        await update.message.reply_text(f"No task [{tid}] found.")


async def cmd_priority(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ctx.args or len(ctx.args) < 2:
        await update.message.reply_text(
            "Usage: `/priority [id] [1-3]`\n1=high 🔴  2=medium 🟡  3=low 🟢",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        tid, pri = int(ctx.args[0]), int(ctx.args[1])
        assert 1 <= pri <= 3
    except (ValueError, AssertionError):
        await update.message.reply_text("Task ID and priority (1-3) must be numbers.")
        return
    if db.set_priority(uid, tid, pri):
        label = {1: "🔴 High", 2: "🟡 Medium", 3: "🟢 Low"}[pri]
        await update.message.reply_text(f"Task [{tid}] priority set to {label}.")
    else:
        await update.message.reply_text(f"No task [{tid}] found.")


async def cmd_remind(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ctx.args or len(ctx.args) < 2:
        await update.message.reply_text(
            "Usage: `/remind [id] [time]`\n"
            "Examples:\n"
            "  `/remind 3 14:30`\n"
            "  `/remind 3 in 2 hours`\n"
            "  `/remind 3 in 30 minutes`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("Task ID must be a number.")
        return

    time_str = " ".join(ctx.args[1:])
    remind_at = parse_time(time_str)
    if not remind_at:
        await update.message.reply_text(
            f"Couldn't parse time: `{time_str}`\n"
            "Try `14:30`, `in 2 hours`, or `in 30 minutes`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    tasks = db.list_tasks(uid)
    if not any(t["id"] == tid for t in tasks):
        await update.message.reply_text(f"No pending task [{tid}] found.")
        return

    db.add_reminder(uid, tid, remind_at)
    await update.message.reply_text(
        f"⏰ Reminder set for [{tid}] at {remind_at.strftime('%a %d %b %H:%M')}.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_reminders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rows = db.list_reminders(uid)
    if not rows:
        await update.message.reply_text("No upcoming reminders.")
        return
    lines = ["*Upcoming reminders:*\n"]
    for r in rows:
        t = datetime.fromisoformat(r["remind_at"])
        lines.append(f"⏰ {t.strftime('%a %d %b %H:%M')} — [{r['task_id']}] {r['title']}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── Reminder Scheduler ─────────────────────────────────────────────────────

async def fire_reminders(app: Application):
    due = db.get_due_reminders()
    for r in due:
        if r["done"]:
            db.mark_reminder_fired(r["id"])
            continue
        try:
            await app.bot.send_message(
                chat_id=r["user_id"],
                text=f"⏰ *Reminder!*\n[{r['task_id']}] {r['title']}\n\n/next for what to do now.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.warning(f"Failed to send reminder {r['id']}: {e}")
        db.mark_reminder_fired(r["id"])


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    db.init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("priority", cmd_priority))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plain_message))

    # Check for due reminders every 60 seconds
    scheduler = AsyncIOScheduler()
    scheduler.add_job(fire_reminders, "interval", seconds=60, args=[app])
    scheduler.start()

    logger.info("Bot started. Polling for updates...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
