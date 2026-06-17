"""
discord_bot.py — Discord DM integration for TaskBot
----------------------------------------------------
DM your Discord bot from phone or PC to add tasks and get nudges.
Run alongside app.py:
  python discord_bot.py

Requires: discord.py  (pip install discord.py)
Set env var: DISCORD_TOKEN=your_bot_token
"""

import os
import discord
from discord.ext import commands

# Import the same chat parser from the main app
import sys
sys.path.insert(0, os.path.dirname(__file__))
from app import parse_chat, build_nudge, _send_push_all
import database as db

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
# Your Discord user ID — only YOU can DM the bot and get responses
# Get it: Discord > Settings > Advanced > Developer Mode > right-click your name > Copy ID
ALLOWED_USER_ID = int(os.environ.get("DISCORD_USER_ID", "0"))

USER_ID = 1  # same single-user ID as app.py

intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


def strip_markdown(text: str) -> str:
    """Convert app's markdown to plain text for Discord (Discord has its own md)."""
    return text.replace("**", "**").replace("*", "*")  # Discord supports ** and * natively


@bot.event
async def on_ready():
    print(f"Discord bot logged in as {bot.user} (ID: {bot.user.id})")
    print("Send a DM to the bot from your Discord account to manage tasks.")


@bot.event
async def on_message(message: discord.Message):
    # Only respond to DMs, only from you
    if not isinstance(message.channel, discord.DMChannel):
        return
    if message.author.bot:
        return
    if ALLOWED_USER_ID and message.author.id != ALLOWED_USER_ID:
        await message.channel.send("⛔ Not authorised.")
        return

    text = message.content.strip()
    if not text:
        return

    db.init_db()
    intent = parse_chat(text)
    action = intent["intent"]

    async with message.channel.typing():
        if action == "add":
            tid = db.add_task(USER_ID, intent["title"], intent["category"], intent["priority"])
            cat = "💻 IT" if intent["category"] == "it" else "📋 General"
            reply = f"✅ Added **[{tid}] {intent['title']}** · {cat}"
            if intent.get("remind_at"):
                db.add_reminder(USER_ID, tid, intent["remind_at"])
                reply += f"\n⏰ Reminder: {intent['remind_at'].strftime('%a %d %b %H:%M')}"

        elif action == "add_with_reminder":
            from app import detect_category
            cat = detect_category(intent["title"])
            tid = db.add_task(USER_ID, intent["title"], cat)
            reply = f"✅ Added **[{tid}] {intent['title']}**"
            if intent.get("remind_at"):
                db.add_reminder(USER_ID, tid, intent["remind_at"])
                reply += f"\n⏰ Reminder: {intent['remind_at'].strftime('%a %d %b %H:%M')}"

        elif action == "remind_last":
            tasks = db.list_tasks(USER_ID)
            if tasks:
                last = tasks[-1]
                db.add_reminder(USER_ID, last["id"], intent["remind_at"])
                reply = f"⏰ Reminder set for **[{last['id']}] {last['title']}** at {intent['remind_at'].strftime('%H:%M')}"
            else:
                reply = "No tasks yet to set a reminder on."

        elif action == "list":
            tasks = db.list_tasks(USER_ID)
            if not tasks:
                reply = "🎉 No pending tasks! You're all caught up."
            else:
                pri = {1:"🔴",2:"🟡",3:"🟢"}
                cat = {"it":"💻","general":"📋","personal":"👤"}
                lines = [f"**{len(tasks)} pending tasks:**\n"]
                for t in tasks:
                    due = f" · ⏰ `{t['due_at'][:16]}`" if t["due_at"] else ""
                    lines.append(f"{pri.get(t['priority'],'⚪')} {cat.get(t['category'],'📋')} `[{t['id']}]` {t['title']}{due}")
                lines.append("\nSay `next` for what to do first.")
                reply = "\n".join(lines)

        elif action == "next":
            task = db.get_next_task(USER_ID)
            reply = build_nudge(task) if task else "🎉 All done! No pending tasks."

        elif action == "done":
            ok = db.mark_done(USER_ID, intent["task_id"])
            if ok:
                reply = f"✅ Task [{intent['task_id']}] complete!"
                nxt = db.get_next_task(USER_ID)
                if nxt:
                    reply += f"\n➡️ Next: **[{nxt['id']}] {nxt['title']}** · say `next` for nudge."
            else:
                reply = f"Task [{intent['task_id']}] not found."

        elif action == "prompt_done":
            reply = "Which task? Say `done 3` (replace 3 with the task number)."

        elif action == "delete":
            ok = db.delete_task(USER_ID, intent["task_id"])
            reply = f"🗑️ Deleted [{intent['task_id']}]." if ok else f"No task [{intent['task_id']}] found."

        elif action == "priority":
            ok = db.set_priority(USER_ID, intent["task_id"], intent["priority"])
            label = {1:"🔴 High",2:"🟡 Medium",3:"🟢 Low"}.get(intent["priority"],"")
            reply = f"Task [{intent['task_id']}] → {label}" if ok else "Task not found."

        else:
            reply = "Didn't catch that. Try: `add patch server`, `list`, `next`, `done 3`, or `remind me at 14:30`."

    await message.channel.send(reply)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("ERROR: Set DISCORD_TOKEN environment variable")
        exit(1)
    db.init_db()
    bot.run(DISCORD_TOKEN)
