import os
import asyncio
import threading
import re
import time
import shutil  # Import the shutil module for directory removal
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = "8443350946:AAHmWA5jxxX3HfuxmZtMhQw_nHU-4f-EjVk"  # Replace with your bot's token
ADMIN_CHAT_ID = "7962617461"          # Replace with your Telegram chat ID

# --- PROGRESS BAR WRAPPER for UPLOADING ---

class ProgressCallbackFile:
    def __init__(self, filepath, loop, context, chat_id, message_id):
        self._filepath = filepath
        self._loop = loop
        self._context = context
        self._chat_id = chat_id
        self._message_id = message_id
        self._file = open(filepath, 'rb')
        self._total_size = os.path.getsize(filepath)
        self._bytes_read = 0
        self._last_update_time = 0

    def read(self, size=-1):
        data = self._file.read(size)
        if data:
            self._bytes_read += len(data)
            current_time = time.time()
            if current_time - self._last_update_time > 2:  # Update every 2 seconds
                self._last_update_time = current_time
                asyncio.run_coroutine_threadsafe(
                    self._update_telegram_message(), self._loop
                )
        return data

    async def _update_telegram_message(self):
        try:
            percent = (self._bytes_read / self._total_size) * 100
            progress_bar = f"[{'█' * int(percent // 10)}{' ' * (10 - int(percent // 10))}]"
            read_mb = self._bytes_read / 1024 / 1024
            total_mb = self._total_size / 1024 / 1024
            
            progress_text = (
                f"**Uploading...**\n"
                f"{progress_bar} {percent:.1f}%\n"
                f"📤 {read_mb:.2f}MB / {total_mb:.2f}MB"
            )
            await self._context.bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=self._message_id,
                text=progress_text,
                parse_mode='Markdown'
            )
        except Exception:
            pass
            
    def __getattr__(self, name):
        return getattr(self._file, name)
        
    def __len__(self):
        return self._total_size
        
    def close(self):
        self._file.close()


# --- BOT COMMANDS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome! Use /dl <URL> to download a song.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🎵 **Available Command** 🎵\n\n"
        "`/dl <Spotify or YouTube URL>` - Downloads the audio (and lyrics if found).\n\n"
        "`/messageadmin <message>` - Send a message to the bot admin."
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def message_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Please provide a message. Usage: /messageadmin <your message>")
        return
    message_text = ' '.join(context.args)
    user_info = update.effective_user
    forward_text = f"Message from user @{user_info.username} (ID: {user_info.id}):\n\n{message_text}"
    try:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=forward_text)
        await update.message.reply_text("Your message has been sent to the admin.")
    except Exception as e:
        print(f"Error sending message to admin: {e}")
        await update.message.reply_text("Sorry, I could not send your message.")

# --- DOWNLOAD LOGIC ---

async def read_stream_and_update_progress(stream, context, chat_id, message_id):
    """Reads stdout from subprocess and updates the download progress message."""
    last_update_time = 0
    while not stream.at_eof():
        line_bytes = await stream.readline()
        if not line_bytes:
            break
        line = line_bytes.decode('utf-8', errors='ignore').strip()

        match = re.search(
            r'\[download\]\s+(?P<percent>\d+\.\d+)% of\s+(?P<size>~?\d+\.\d+\w+)\s+at\s+(?P<speed>.*?)\s+ETA\s+(?P<eta>.*)',
            line
        )
        
        if match:
            current_time = time.time()
            if current_time - last_update_time > 2:
                last_update_time = current_time
                percent = float(match.group('percent'))
                progress_bar = f"[{'█' * int(percent // 10)}{' ' * (10 - int(percent // 10))}]"
                
                progress_text = (
                    f"**Downloading...**\n"
                    f"{progress_bar} {percent:.1f}%\n"
                    f"📥 Size: {match.group('size')}\n"
                    f"⚡️ Speed: {match.group('speed')}\n"
                    f"⏳ ETA: {match.group('eta')}"
                )
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=message_id, text=progress_text, parse_mode='Markdown'
                    )
                except Exception:
                    pass

def run_download_in_thread(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(download_and_upload(update, context, url))

async def download_and_upload(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    status_message = await update.message.reply_text("🚀 Initializing...")
    chat_id = update.effective_chat.id
    files_before = set(os.listdir('.'))
    
    try:
        # ===== FIX 1: Execute spotdl as a module to ensure it's found =====
        command = (
            f'python -m spotdl download "{url}" --lyrics genius --ignore-albums '
            '--yt-dlp-args "--cookies cookies.txt" --format mp3 --bitrate 320k'
        )
        
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        await read_stream_and_update_progress(process.stdout, context, chat_id, status_message.message_id)
        await process.wait()

        if process.returncode != 0:
            stderr_output = (await process.stderr.read()).decode()
            print(f"Error downloading: {stderr_output}")
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=status_message.message_id, text=f"❌ Download failed.\n`{stderr_output}`"
            )
            return

        files_after = set(os.listdir('.'))
        new_files = files_after - files_before
        
        if not new_files:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=status_message.message_id, text="❌ Download finished, but no new files were found."
            )
            return
        
        current_loop = asyncio.get_running_loop()

        for filename in new_files:
            try:
                progress_wrapper = ProgressCallbackFile(
                    filename, current_loop, context, chat_id, status_message.message_id
                )
                if filename.endswith(".mp3"):
                    await context.bot.send_audio(chat_id=chat_id, audio=progress_wrapper)
                elif filename.endswith(".lrc"):
                    await context.bot.send_document(chat_id=chat_id, document=progress_wrapper)
            except Exception as e:
                print(f"Error uploading {filename}: {e}")
                await context.bot.send_message(chat_id=chat_id, text=f"Could not upload {filename}.")
            finally:
                progress_wrapper.close()

        await context.bot.delete_message(chat_id=chat_id, message_id=status_message.message_id)

    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=status_message.message_id, text="❌ An unexpected error occurred."
        )
    
    finally:
        # ===== FIX 2: Correctly remove files and directories =====
        files_after_cleanup = set(os.listdir('.'))
        files_to_delete = files_after_cleanup - files_before
        for filename in files_to_delete:
            try:
                if os.path.isdir(filename):
                    shutil.rmtree(filename)  # Use this for directories
                else:
                    os.remove(filename)      # Use this for files
            except OSError as e:
                print(f"Error deleting {filename}: {e}")

async def download_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Please provide a URL. Usage: /dl <url>")
        return
    url = context.args[0]
    threading.Thread(target=run_download_in_thread, args=(update, context, url)).start()

def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("messageadmin", message_admin))
    application.add_handler(CommandHandler("dl", download_handler))
    print("Bot is running...")
    application.run_polling()

if __name__ == '__main__':
    main()
