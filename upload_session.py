import os
import tempfile
import shutil
import asyncio
import logging
from datetime import datetime
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from github import Github
from github.GithubException import GithubException

# ==================== تنظیمات از متغیرهای محیطی ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", 0))
GH_TOKEN = os.getenv("GH_TOKEN")          # ← نام تغییر کرد
REPO_NAME = os.getenv("REPO_NAME")        # به صورت "owner/repo"

if not all([BOT_TOKEN, OWNER_ID, GH_TOKEN, REPO_NAME]):
    raise ValueError("لطفاً تمام متغیرهای محیطی را تنظیم کنید: BOT_TOKEN, OWNER_ID, GH_TOKEN, REPO_NAME")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== کلاس جلسه ====================
class Session:
    def __init__(self):
        self.temp_dir = tempfile.mkdtemp(prefix="telegram_upload_")
        self.files = []          # هر عنصر: {"name": str, "file_id": str, "size": int, "local_path": str}
        self.status_message_id = None
        self.chat_id = None
        # پوشه خودکار بر اساس تاریخ و زمان جلسه
        self.folder_name = datetime.now().strftime("uploads/%Y-%m-%d_%H-%M-%S/")

session = Session()
bot = Bot(token=BOT_TOKEN)
github = Github(GH_TOKEN)                 # ← استفاده از GH_TOKEN
repo = github.get_repo(REPO_NAME)

# ==================== توابع کمکی ====================
def get_file_size_str(size_bytes):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"

async def update_status_message():
    """ویرایش پیام وضعیت با لیست فایل‌ها و دکمه‌ها"""
    if not session.status_message_id or not session.chat_id:
        return
    text = "📂 **لیست فایل‌های آماده آپلود**\n"
    text += f"🗂 پوشه مقصد: `{session.folder_name}`\n\n"
    if not session.files:
        text += "هیچ فایلی ارسال نشده است.\n"
    else:
        for idx, f in enumerate(session.files, 1):
            text += f"{idx}. {f['name']} ({get_file_size_str(f['size'])})\n"
    text += f"\n📌 تعداد کل: {len(session.files)}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 آپلود به گیت‌هاب", callback_data="upload")],
        [InlineKeyboardButton("❌ لغو و خاموش شدن", callback_data="cancel")],
        [InlineKeyboardButton("🗑 حذف آخرین فایل", callback_data="remove_last"),
         InlineKeyboardButton("🧹 پاک کردن همه", callback_data="clear_all")]
    ])
    await bot.edit_message_text(
        chat_id=session.chat_id,
        message_id=session.status_message_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=keyboard
    )

async def download_file(file_id, file_name):
    """دانلود فایل از تلگرام در دایرکتوری موقت و بازگرداندن مسیر محلی"""
    local_path = os.path.join(session.temp_dir, file_name)
    file = await bot.get_file(file_id)
    await file.download_to_drive(local_path)
    return local_path

async def upload_to_github_commit(file_path, file_name):
    """آپلود فایل کوچک (≤100MB) با commit در مسیر پوشه جلسه"""
    remote_path = f"{session.folder_name}{file_name}"
    with open(file_path, "rb") as f:
        content = f.read()
    try:
        contents = repo.get_contents(remote_path)
        repo.update_file(contents.path, f"Update {file_name}", content, contents.sha, branch="main")
        logger.info(f"Updated {remote_path}")
    except GithubException as e:
        if e.status == 404:
            repo.create_file(remote_path, f"Upload {file_name}", content, branch="main")
            logger.info(f"Created {remote_path}")
        else:
            raise
    return f"https://raw.githubusercontent.com/{REPO_NAME}/main/{remote_path}"

async def upload_to_github_release(file_path, file_name):
    """آپلود فایل بزرگ (>100MB) به عنوان یک Release Asset"""
    release_tag = f"upload-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    release_name = f"Upload session {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    release = repo.create_git_release(
        tag=release_tag,
        name=release_name,
        message=f"فایل‌های بزرگ آپلود شده در تاریخ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    with open(file_path, "rb") as f:
        asset = release.upload_asset(path=file_name, content=f, content_type="application/octet-stream")
    return asset.browser_download_url

# ==================== هندلرهای تلگرام ====================
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دریافت فایل جدید از کاربر مجاز"""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("شما مجاز به استفاده از این بات نیستید.")
        return
    doc = update.message.document
    if doc.file_size > 2 * 1024 * 1024 * 1024:
        await update.message.reply_text("❌ حجم فایل بیشتر از ۲ گیگابایت است و قابل آپلود به گیت‌هاب نیست.")
        return
    local_path = await download_file(doc.file_id, doc.file_name)
    session.files.append({
        "name": doc.file_name,
        "file_id": doc.file_id,
        "size": doc.file_size,
        "local_path": local_path
    })
    if not session.status_message_id:
        session.chat_id = update.effective_chat.id
        msg = await update.message.reply_text("در حال آماده‌سازی...")
        session.status_message_id = msg.message_id
        await update_status_message()
    else:
        await update_status_message()
    await update.message.delete()

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if update.effective_user.id != OWNER_ID:
        await query.edit_message_text("شما مجاز نیستید.")
        return
    data = query.data

    if data == "upload":
        if not session.files:
            await query.edit_message_text("هیچ فایلی برای آپلود وجود ندارد.")
            return
        await query.edit_message_text("🔄 در حال آپلود فایل‌ها به گیت‌هاب...")
        results = []
        for f in session.files:
            try:
                if f["size"] > 100 * 1024 * 1024:
                    url = await upload_to_github_release(f["local_path"], f["name"])
                    results.append(f"✅ {f['name']} (بزرگ) → آپلود شد در Release: {url}")
                else:
                    url = await upload_to_github_commit(f["local_path"], f["name"])
                    results.append(f"✅ {f['name']} → commit شد: {url}")
            except Exception as e:
                logger.error(f"خطا در آپلود {f['name']}: {e}")
                results.append(f"❌ {f['name']} → خطا: {str(e)}")
        result_text = "**نتیجه آپلود:**\n" + "\n".join(results)
        await query.edit_message_text(result_text, parse_mode="Markdown", disable_web_page_preview=True)
        await finish_session()

    elif data == "cancel":
        await query.edit_message_text("❌ عملیات لغو شد. جلسه خاتمه یافت.")
        await finish_session()

    elif data == "remove_last":
        if session.files:
            removed = session.files.pop()
            try:
                os.remove(removed["local_path"])
            except OSError:
                pass
            await update_status_message()
        else:
            await query.answer("لیست خالی است!")

    elif data == "clear_all":
        for f in session.files:
            try:
                os.remove(f["local_path"])
            except OSError:
                pass
        session.files.clear()
        await update_status_message()
        await query.answer("همه فایل‌ها پاک شدند.")

async def finish_session():
    shutil.rmtree(session.temp_dir, ignore_errors=True)
    if session.chat_id:
        await bot.send_message(chat_id=session.chat_id, text="👋 جلسه پایان یافت. ربات خاموش می‌شود.")
    os._exit(0)

async def idle_timeout():
    await asyncio.sleep(300)
    if session.chat_id:
        await bot.send_message(chat_id=session.chat_id, text="⏰ عدم فعالیت به مدت ۵ دقیقه. جلسه خاتمه یافت.")
    await finish_session()

async def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CallbackQueryHandler(handle_callback))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    asyncio.create_task(idle_timeout())

    while True:
        await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(session.temp_dir, ignore_errors=True)
