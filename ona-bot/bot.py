import os
import requests
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

from dotenv import load_dotenv

#load environment variables

load_dotenv()


BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SHEET_WEBAPP_URL = os.environ["SHEET_WEBAPP_URL"]
SHEET_SECRET = os.environ["SHEET_SECRET"]


def post_to_sheet(payload: dict) -> tuple[bool, str]:
    """
    Returns (ok, debug_msg). We ignore response body because Apps Script may redirect.
    """
    try:
        r = requests.post(
            SHEET_WEBAPP_URL,
            json=payload,
            timeout=15,
            allow_redirects=True,
            headers={"Content-Type": "application/json"},
        )
        if 200 <= r.status_code < 300:
            return True, f"HTTP {r.status_code}"
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"EXC: {e}"


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_id = str(update.effective_user.id) if update.effective_user else "unknown"
    text = update.message.text.strip()
    ts = datetime.now(timezone.utc).isoformat()

    payload = {
        "secret": SHEET_SECRET,
        "timestamp": ts,
        "user_id": user_id,
        "raw_text": text,
        "source": "telegram_text",
    }

    ok, dbg = post_to_sheet(payload)
    await update.message.reply_text("Logged ✅" if ok else f"Failed ❌ ({dbg})")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # For MVP we don’t transcribe. We just record metadata so you see it came through.
    if not update.message or not update.message.voice:
        return

    user_id = str(update.effective_user.id) if update.effective_user else "unknown"
    ts = datetime.now(timezone.utc).isoformat()

    duration = update.message.voice.duration
    file_id = update.message.voice.file_id

    payload = {
        "secret": SHEET_SECRET,
        "timestamp": ts,
        "user_id": user_id,
        "raw_text": f"[VOICE] duration_sec={duration} file_id={file_id}",
        "source": "telegram_voice",
    }

    ok, dbg = post_to_sheet(payload)
    await update.message.reply_text("Voice logged ✅" if ok else f"Failed ❌ ({dbg})")


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    print("Bot running… send a message in Telegram.")
    app.run_polling()

print("ENV OK:", bool(os.environ.get("TELEGRAM_BOT_TOKEN")))

if __name__ == "__main__":
    main()
