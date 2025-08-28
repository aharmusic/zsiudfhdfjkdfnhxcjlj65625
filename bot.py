import os
import asyncio
import threading
import re
import time
import shutil
import sys
from queue import Queue
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from flask import Flask
from mutagen.mp3 import MP3
from mutagen.id3 import TIT2, TPE1

# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = "8443350946:AAHmWA5jxxX3HfuxmZtMhQw_nHU-4f-EjVk"
ADMIN_CHAT_ID = "7962617461"
PORT = int(os.environ.get('PORT', 8080))

# --- 1. THE WEB SERVER (TO KEEP KOYEB HAPPY) ---
flask_app = Flask(__name__)
@flask_app.route('/')
def health_check():
    return "Bot is alive and running!", 200
def run_flask():
    flask_app.run(host='0.0.0.0', port=PORT)

# --- 2. THE TELEGRAM BOT LOGIC ---

# === NEW: THE DOWNLOAD QUEUE ===
download_queue = Queue()

class ProgressCallbackFile:
    def __init__(self, filepath, loop, context, chat_id, message_id):
        self._filepath, self._loop, self._context = filepath, loop, context
        self._chat_id, self._message_id = chat_id, message_id
        self._file = open(filepath, 'rb'); self._total_size = os.path.getsize(filepath)
        self._bytes_read, self._last_update_time = 0, 0
    def read(self, size=-1):
        data = self._file.read(size)
        if data:
            self._bytes_read += len(data); current_time = time.time()
            if current_time - self._last_update_time > 2:
                self._last_update_time = current_time
                asyncio.run_coroutine_threadsafe(self._update_telegram_message(), self._loop)
        return data
    async def _update_telegram_message(self):
        try:
            percent = (self._bytes_read / self._total_size) * 100 if self._total_size > 0 else 0
            progress_bar = f"[{'█' * int(percent // 10)}{' ' * (10 - int(percent // 10))}]"
            read_mb = self._bytes_read / 1024 / 1024; total_mb = self._total_size / 1024 / 1024
            progress_text = (f"**Uploading...**\n{progress_bar} {percent:.1f}%\n📤 {read_mb:.2f}MB / {total_mb:.2f}MB")
            await self._context.bot.edit_message_text(chat_id=self._chat_id, message_id=self._message_id, text=progress_text, parse_mode='Markdown')
        except Exception: pass
    def __getattr__(self, name): return getattr(self._file, name)
    def __len__(self): return self._total_size
    def close(self): self._file.close()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wel_msg = ("Welcome! Use /dl <URL> to download a song.\n (YoutubeMusic Links Aren't Supported)")
    await update.message.reply_text(wel_msg, parse_mode='Markdown')
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = ("🎵 **Available Command** 🎵\n\n`/dl <Spotify or YouTube URL>` - Downloads the audio (and lyrics if found).\n\n`/messageadmin <message>` - Send a message to the bot admin.")
    await update.message.reply_text(help_text, parse_mode='Markdown')
async def message_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: await update.message.reply_text("Please provide a message. Usage: /messageadmin <your message>"); return
    message_text = ' '.join(context.args); user_info = update.effective_user
    forward_text = f"Message from user @{user_info.username} (ID: {user_info.id}):\n\n{message_text}"
    try: await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=forward_text); await update.message.reply_text("Your message has been sent to the admin.")
    except Exception as e: print(f"Error sending message to admin: {e}"); await update.message.reply_text("Sorry, I could not send your message.")

async def log_and_parse_stream(stream, context, chat_id, message_id):
    last_update_time = 0
    while not stream.at_eof():
        line_bytes = await stream.readline();
        if not line_bytes: break
        line = line_bytes.decode('utf-8', errors='ignore').strip()
        print(f"[SPOTDL_INFO] {line}")
        match = re.search(r'\[download\]\s+(?P<percent>\d+\.\d+)% of\s+(?P<size>~?\d+\.\d+\w+)\s+at\s+(?P<speed>.*?)\s+ETA\s+(?P<eta>.*)', line)
        if match:
            current_time = time.time()
            if current_time - last_update_time > 2:
                last_update_time = current_time; percent = float(match.group('percent'))
                progress_bar = f"[{'█' * int(percent // 10)}{' ' * (10 - int(percent // 10))}]"
                progress_text = (f"**Downloading...**\n{progress_bar} {percent:.1f}%\n📥 Size: {match.group('size')}\n⚡️ Speed: {match.group('speed')}\n⏳ ETA: {match.group('eta')}")
                try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=progress_text, parse_mode='Markdown')
                except Exception: pass

async def log_stderr_stream(stream):
    while not stream.at_eof():
        line_bytes = await stream.readline();
        if not line_bytes: break
        line = line_bytes.decode('utf-8', errors='ignore').strip()
        print(f"[SPOTDL_ERROR] {line}")

async def download_and_upload(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    status_message = await update.message.reply_text("🚀 Your request is in the queue...")
    chat_id = update.effective_chat.id; files_before = set(os.listdir('.'))
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=status_message.message_id, text="🔥 Your download is starting now...")
        python_executable = sys.executable
        # === NEW CPU-THROTTLED COMMAND ===
        # We pass postprocessor args to yt-dlp to limit ffmpeg's threads
        command = (f'{python_executable} -m spotdl download "{url}" --lyrics genius --ignore-albums '
                   '--format mp3 --bitrate 320k --no-cache '
                   '--yt-dlp-args "--postprocessor-args \\"ffmpeg:-threads 1\\""')
        
        process = await asyncio.create_subprocess_shell(command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout_task = asyncio.create_task(log_and_parse_stream(process.stdout, context, chat_id, status_message.message_id))
        stderr_task = asyncio.create_task(log_stderr_stream(process.stderr))
        await process.wait(); await asyncio.gather(stdout_task, stderr_task)

        if process.returncode != 0: await context.bot.edit_message_text(chat_id=chat_id, message_id=status_message.message_id, text="❌ Download failed. Check logs."); return
        files_after = set(os.listdir('.')); new_files = files_after - files_before
        mp3_file_path = next((f for f in new_files if f.endswith('.mp3')), None)
        lrc_file_path = next((f for f in new_files if f.endswith('.lrc')), None)
        if not mp3_file_path: await context.bot.edit_message_text(chat_id=chat_id, message_id=status_message.message_id, text="❌ MP3 file not found."); return

        try:
            audio = MP3(mp3_file_path); duration = int(audio.info.length)
            title = str(audio.get('TIT2', "Unknown Title")); artist = str(audio.get('TPE1', "Unknown Artist"))
        except Exception as e: print(f"Mutagen error: {e}"); duration, title, artist = None, "Unknown", "Unknown"
        
        current_loop = asyncio.get_running_loop()
        progress_wrapper_audio = ProgressCallbackFile(mp3_file_path, current_loop, context, chat_id, status_message.message_id)
        try: await context.bot.send_audio(chat_id=chat_id, audio=progress_wrapper_audio, duration=duration, title=title, performer=artist)
        finally: progress_wrapper_audio.close()
        if lrc_file_path:
            with open(lrc_file_path, 'rb') as lrc_file: await context.bot.send_document(chat_id=chat_id, document=lrc_file)
        await context.bot.delete_message(chat_id=chat_id, message_id=status_message.message_id)
    except Exception as e: print(f"Unexpected error: {e}"); await context.bot.edit_message_text(chat_id=chat_id, message_id=status_message.message_id, text="❌ An error occurred.")
    finally:
        files_after_cleanup = set(os.listdir('.')); files_to_delete = files_after_cleanup - files_before
        for filename in files_to_delete:
            try:
                if os.path.isdir(filename): shutil.rmtree(filename)
                else: os.remove(filename)
            except OSError as e: print(f"Error deleting {filename}: {e}")

# === NEW: DOWNLOAD WORKER THREAD ===
def download_worker():
    """Pulls tasks from the queue and executes them one by one."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while True:
        update, context, url = download_queue.get()
        print(f"Worker starting new download for chat {update.effective_chat.id}")
        loop.run_until_complete(download_and_upload(update, context, url))
        download_queue.task_done()

async def download_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Adds a new download request to the queue."""
    if not context.args: await update.message.reply_text("Please provide a URL."); return
    url = context.args[0]
    # Put the necessary objects into the queue
    download_queue.put((update, context, url))
    await update.message.reply_text("✅ Your request has been added to the download queue.")

def run_bot():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("messageadmin", message_admin))
    application.add_handler(CommandHandler("dl", download_handler))
    print("Bot is running...")
    application.run_polling()

# --- 3. THE MAIN STARTER ---
if __name__ == '__main__':
    # Start the Flask web server in a background thread
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # Start the single download worker in a background thread
    worker_thread = threading.Thread(target=download_worker)
    worker_thread.daemon = True
    worker_thread.start()

    # Run the bot in the main thread
    run_bot()
