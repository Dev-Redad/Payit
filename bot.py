import os
import logging
import requests
import json
import time
import sqlite3
from functools import wraps
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode
from telegram.ext import (
    Updater, CommandHandler, MessageHandler,
    Filters, CallbackContext, ConversationHandler, CallbackQueryHandler
)

# --- Basic Logging Setup ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ==============================================================================
# --- MAIN CONFIGURATION - EDIT YOUR SETTINGS HERE ---
# ==============================================================================

# --- Telegram Bot ---
TOKEN = "7729759194:AAHBaEkkH72krZPnThl6BS93-oEf_RGqeSQ"
ADMIN_IDS = [7223414109, 6053105336, 7381642564]
STORAGE_CHANNEL_ID = -1002736992756
# Add the ID of your private channel here to receive sales logs
LOG_CHANNEL_ID = 0 # Example: -100123456789

# --- Feature Toggles & Settings ---
FORCE_SUBSCRIBE_CHANNEL_IDS = [] 
FORCE_SUBSCRIBE_ENABLED = True
PROTECT_CONTENT_ENABLED = False

# --- Razorpay (Live Mode) ---
RAZORPAY_KEY_ID = "rzp_live_Kfvz8iobE8iUZc"
RAZORPAY_KEY_SECRET = "bcPhJQ2pHTaaF94FhWCEl6eD"

# ==============================================================================
# --- END OF CONFIGURATION ---
# ==============================================================================

# --- Dynamic & Static Configs ---
CATALOG_FILE = "catalog.json"
DATABASE_FILE = "bot_database.db"
CONFIG_FILE = "config.json"
FILE_CATALOG = {}
BOT_CONFIG = {}

# --- Conversation States ---
GET_PRODUCT_FILES, PRICE, GET_BROADCAST_FILES, GET_BROADCAST_TEXT, BROADCAST_CONFIRM, DELETE_OPTION, GET_DELETE_TIME, GET_FS_PHOTO, GET_FS_TEXT, GET_START_PHOTO, GET_START_TEXT = range(11)

# --- Config & Data Functions ---
def load_bot_config():
    global BOT_CONFIG
    try:
        with open(CONFIG_FILE, "r") as f: BOT_CONFIG = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        BOT_CONFIG = {
            "force_sub_photo_id": None, "force_sub_text": "You must join our channel(s) to use this bot.",
            "welcome_photo_id": None, "welcome_text": "Welcome back!"
        }
        save_bot_config()

def save_bot_config():
    with open(CONFIG_FILE, "w") as f: json.dump(BOT_CONFIG, f, indent=4)

def setup_database():
    conn = sqlite3.connect(DATABASE_FILE, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT)")
    # New table to track users whose first purchase has been logged
    cursor.execute("CREATE TABLE IF NOT EXISTS logged_buyers (user_id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

def add_user_to_db(user_id: int, username: str):
    conn = sqlite3.connect(DATABASE_FILE, check_same_thread=False)
    conn.cursor().execute("INSERT OR REPLACE INTO users (user_id, username) VALUES (?, ?)", (user_id, username)); conn.commit(); conn.close()

def is_buyer_logged(user_id: int):
    conn = sqlite3.connect(DATABASE_FILE, check_same_thread=False)
    result = conn.cursor().execute("SELECT user_id FROM logged_buyers WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return result is not None

def log_new_buyer(user_id: int):
    conn = sqlite3.connect(DATABASE_FILE, check_same_thread=False)
    conn.cursor().execute("INSERT OR IGNORE INTO logged_buyers (user_id) VALUES (?)", (user_id,)); conn.commit(); conn.close()

def get_user_from_db(user_id: int):
    conn = sqlite3.connect(DATABASE_FILE, check_same_thread=False)
    result = conn.cursor().execute("SELECT username FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return result[0] if result and result[0] else None

def get_all_user_ids():
    conn = sqlite3.connect(DATABASE_FILE, check_same_thread=False)
    return [row[0] for row in conn.cursor().execute("SELECT user_id FROM users").fetchall()]

def load_catalog():
    global FILE_CATALOG
    try:
        with open(CATALOG_FILE, "r") as f: FILE_CATALOG = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        FILE_CATALOG = {}; save_catalog()

def save_catalog():
    with open(CATALOG_FILE, "w") as f: json.dump(FILE_CATALOG, f, indent=4)

# --- Decorator for Force Subscribe ---
def force_subscribe(func):
    @wraps(func)
    def wrapper(update: Update, context: CallbackContext, *args, **kwargs):
        if not FORCE_SUBSCRIBE_ENABLED or not FORCE_SUBSCRIBE_CHANNEL_IDS or update.effective_user.id in ADMIN_IDS:
            return func(update, context, *args, **kwargs)

        user_id = update.effective_user.id
        channels_to_join = []
        for channel_id in FORCE_SUBSCRIBE_CHANNEL_IDS:
            try:
                member = context.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
                if member.status not in ['member', 'administrator', 'creator']:
                    channels_to_join.append(channel_id)
            except Exception as e:
                logger.warning(f"Could not check membership for user {user_id} in channel {channel_id}: {e}.")
                channels_to_join.append(channel_id)

        if not channels_to_join: return func(update, context, *args, **kwargs)

        context.user_data['pending_command'] = {'func': func, 'update': update}
        buttons = []
        for channel_id in channels_to_join:
            try:
                channel = context.bot.get_chat(channel_id)
                invite_link = channel.invite_link or context.bot.export_chat_invite_link(channel_id)
                buttons.append([InlineKeyboardButton(f"Join {channel.title}", url=invite_link)])
            except Exception as e:
                logger.error(f"Could not get invite link for channel {channel_id}: {e}")
        
        if not buttons:
            update.effective_message.reply_text("Error: Could not retrieve join links. Please contact an admin.")
            return

        buttons.append([InlineKeyboardButton("✅ Joined", callback_data="check_join")])
        
        chat_id = update.effective_chat.id
        photo_id = BOT_CONFIG.get("force_sub_photo_id")
        text = BOT_CONFIG.get("force_sub_text")

        if photo_id: context.bot.send_photo(chat_id=chat_id, photo=photo_id, caption=text, reply_markup=InlineKeyboardMarkup(buttons))
        elif text: context.bot.send_message(chat_id=chat_id, text=text, reply_markup=InlineKeyboardMarkup(buttons))
        else: context.bot.send_message(chat_id=chat_id, text="Please join our channel(s) to continue.", reply_markup=InlineKeyboardMarkup(buttons))
        return
    return wrapper

def check_join_callback(update: Update, context: CallbackContext):
    query = update.callback_query; user_id = query.from_user.id
    channels_to_join = []
    for channel_id in FORCE_SUBSCRIBE_CHANNEL_IDS:
        try:
            member = context.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                channels_to_join.append(channel_id)
        except Exception as e:
            logger.warning(f"Could not check membership for user {user_id} in channel {channel_id}: {e}.")
            channels_to_join.append(channel_id)

    if not channels_to_join:
        query.message.delete(); query.answer("Thank you for joining!", show_alert=True)
        pending_command = context.user_data.pop('pending_command', None)
        if pending_command: return pending_command['func'](pending_command['update'], context)
    else:
        query.answer("You still haven't joined all the required channels.", show_alert=True)

# --- Purchase & Deletion ---
def trigger_purchase_flow(context: CallbackContext, chat_id: int, user_id: int, item_id: str):
    item = FILE_CATALOG.get(item_id)
    if not item: return context.bot.send_message(chat_id, "❌ Sorry, this item could not be found.")
    payload = { "amount": item['price'] * 100, "currency": "INR", "description": f"Payment for Item ID {item_id}", "notes": {"user_id": str(user_id), "item_id": item_id} }
    try:
        resp = requests.post("https://api.razorpay.com/v1/payment_links", auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), json=payload).json()
        payment_id, short_url = resp.get("id"), resp.get("short_url")
        if short_url:
            text = f"Please pay ₹{item['price']} for the file.\n\nClick the button below to proceed. The bot will automatically send you the file once payment is complete."
            button_text = "Click Here to Pay"
            reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton(button_text, url=short_url)]])
            
            payment_message = context.bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

            context.job_queue.run_repeating(check_payment_status, 20, first=20, last=900, context={"payment_id": payment_id, "item_id": item_id, "payment_message_id": payment_message.message_id}, name=f"check_{payment_id}")
    except Exception as e:
        logger.error(f"Razorpay error: {e}"); context.bot.send_message(chat_id, "❌ Payment gateway error.")

def delete_messages_job(context: CallbackContext):
    job = context.job.context
    for msg_id in job.get('message_ids', []):
        try: context.bot.delete_message(chat_id=job['chat_id'], message_id=msg_id)
        except Exception as e: logger.error(f"Delete fail {msg_id}: {e}")

def check_payment_status(context: CallbackContext):
    job = context.job.context; payment_id, item_id, payment_message_id = job["payment_id"], job["item_id"], job["payment_message_id"]
    try:
        url = f"https://api.razorpay.com/v1/payment_links/{payment_id}"; resp = requests.get(url, auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET)).json()
        if resp.get("status") == "paid":
            context.job.schedule_removal(); user_id = int(resp['notes']['user_id']); item = FILE_CATALOG.get(item_id)

            context.job_queue.run_once(delete_messages_job, 5, context={'chat_id': user_id, 'message_ids': [payment_message_id]})
            
            sent_messages = []
            for file_info in item.get('files', []):
                sent_file = context.bot.copy_message(chat_id=user_id, from_chat_id=file_info["channel_id"], message_id=file_info["message_id"], protect_content=PROTECT_CONTENT_ENABLED)
                sent_messages.append(sent_file.message_id); time.sleep(0.5)

            warning = context.bot.send_message(user_id, "⚠️ Files will be deleted in 10 minutes.\nSave them before they get deleted.")
            sent_messages.append(warning.message_id)
            context.job_queue.run_once(delete_messages_job, 600, context={'chat_id': user_id, 'message_ids': sent_messages})
            
            if LOG_CHANNEL_ID:
                if not is_buyer_logged(user_id):
                    username = get_user_from_db(user_id)
                    user_identifier = f"@{username} ({user_id})" if username else f"User ID: {user_id}"
                    notification_text = f"👤 **User:** {user_identifier}\n💰 **Amount:** ₹{item['price']}"
                    try:
                        context.bot.send_message(LOG_CHANNEL_ID, notification_text, parse_mode=ParseMode.MARKDOWN)
                        log_new_buyer(user_id) # Log them only after a successful message send
                    except Exception as e:
                        logger.error(f"Failed to send log message to channel {LOG_CHANNEL_ID}: {e}")

    except Exception as e: logger.error(f"Payment check error: {e}")

# --- User Commands ---
@force_subscribe
def start(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    username = update.effective_user.username
    add_user_to_db(user_id, username)
    message_source = update.message or update.callback_query.message
    if context.args:
        item_id = context.args[0]
        if item_id in FILE_CATALOG:
            trigger_purchase_flow(context, message_source.chat_id, user_id, item_id); return
    
    welcome_text = BOT_CONFIG.get("welcome_text", "Welcome back!")
    photo_id = BOT_CONFIG.get("welcome_photo_id")
    if photo_id:
        message_source.reply_photo(photo=photo_id, caption=welcome_text)
    else:
        message_source.reply_text(welcome_text)

# --- Conversation Handlers ---
def cancel_conversation(update: Update, context: CallbackContext):
    context.user_data.clear()
    update.message.reply_text("Canceled."); return ConversationHandler.END

# --- Product Creation Conversation ---
def add_product_start(update: Update, context: CallbackContext):
    if context.user_data.get('conversation_active'): return
    context.user_data['conversation_active'] = True
    context.user_data['new_product_files'] = []
    update.message.reply_text("Please send the first file for the new product.\n(Send /done when all files are uploaded)"); return GET_PRODUCT_FILES

def get_product_files(update: Update, context: CallbackContext):
    if not update.message.effective_attachment: 
        update.message.reply_text("That's not a file. Please send a document, photo, or video."); return GET_PRODUCT_FILES
    try:
        stored_message = context.bot.forward_message(chat_id=STORAGE_CHANNEL_ID, from_chat_id=update.message.chat_id, message_id=update.message.message_id)
        context.user_data['new_product_files'].append({'channel_id': stored_message.chat_id, 'message_id': stored_message.message_id})
        file_count = len(context.user_data['new_product_files'])
        update.message.reply_text(f"✅ File {file_count} added. Send another file, or /done to finish."); return GET_PRODUCT_FILES
    except Exception as e: 
        logger.error(f"File forward error: {e}"); 
        update.message.reply_text("Error storing file."); 
        context.user_data.clear(); return ConversationHandler.END

def finish_adding_files(update: Update, context: CallbackContext):
    if not context.user_data.get('new_product_files'):
        update.message.reply_text("You haven't added any files yet. Please send a file or /cancel."); return GET_PRODUCT_FILES
    update.message.reply_text("All files received. What is the price for this product (e.g., 10)?"); return PRICE

def get_price(update: Update, context: CallbackContext):
    try: price = float(update.message.text); assert price > 0
    except: update.message.reply_text("Invalid price."); return PRICE
    product_data = { 'price': price, 'files': context.user_data['new_product_files'] }
    item_id = f"item_{int(time.time())}"; FILE_CATALOG[item_id] = product_data; save_catalog()
    deep_link = f"https://t.me/{context.bot.username}?start={item_id}"
    update.message.reply_text(f"✅ Product added successfully!\n\nLink:\n`{deep_link}`", parse_mode=ParseMode.MARKDOWN)
    context.user_data.clear(); return ConversationHandler.END

# --- Broadcast Conversation ---
def broadcast_start(update: Update, context: CallbackContext):
    context.user_data['conversation_active'] = True
    context.user_data['broadcast_files'] = []
    context.user_data['broadcast_text'] = None
    update.message.reply_text("Please send files for the broadcast. Send /done when you have added all files."); return GET_BROADCAST_FILES

def get_broadcast_files(update: Update, context: CallbackContext):
    if update.message.effective_attachment:
        context.user_data['broadcast_files'].append(update.message)
        update.message.reply_text(f"File {len(context.user_data['broadcast_files'])} added. Send another file, or /done."); return GET_BROADCAST_FILES
    else:
        update.message.reply_text("That's not a file. Please send a file or /done."); return GET_BROADCAST_FILES

def finish_broadcast_files(update: Update, context: CallbackContext):
    update.message.reply_text("Files received. Now, send the text message for the broadcast.\n(Send /skip if you only want to send files)"); return GET_BROADCAST_TEXT

def get_broadcast_text(update: Update, context: CallbackContext):
    context.user_data['broadcast_text'] = update.message.text
    return confirm_broadcast(update, context)

def skip_broadcast_text(update: Update, context: CallbackContext):
    return confirm_broadcast(update, context)
    
def confirm_broadcast(update: Update, context: CallbackContext):
    num_files = len(context.user_data.get('broadcast_files', []))
    text_exists = "Yes" if context.user_data.get('broadcast_text') else "No"
    total_users = len(get_all_user_ids())
    
    confirmation_text = (f"You are about to send a broadcast with:\n  - *{num_files}* file(s)\n  - Text message: *{text_exists}*\n\nThis will be sent to *{total_users}* users. Are you sure?")
    buttons = [[InlineKeyboardButton("✅ Yes, Send Now", callback_data="send_broadcast_now")], [InlineKeyboardButton("❌ Cancel", callback_data="cancel_broadcast")]]
    update.message.reply_text(confirmation_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.MARKDOWN); return BROADCAST_CONFIRM

def send_broadcast_now(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); query.edit_message_text("Broadcasting...")
    
    files = context.user_data.get('broadcast_files', [])
    text = context.user_data.get('broadcast_text')
    
    if not files and not text:
        query.message.reply_text("Broadcast canceled as there is no content to send."); 
        context.user_data.clear(); return ConversationHandler.END
        
    user_ids = get_all_user_ids(); success, fail = 0, 0; all_sent_messages = []
    for user_id in user_ids:
        try:
            sent_messages_for_user = []
            if files:
                for file_message in files:
                    sent_msg = context.bot.copy_message(chat_id=user_id, from_chat_id=file_message.chat_id, message_id=file_message.message_id)
                    sent_messages_for_user.append({'chat_id': user_id, 'message_id': sent_msg.message_id})
            if text:
                sent_msg = context.bot.send_message(chat_id=user_id, text=text)
                sent_messages_for_user.append({'chat_id': user_id, 'message_id': sent_msg.message_id})
            
            all_sent_messages.extend(sent_messages_for_user)
            success += 1; time.sleep(0.1)
        except Exception as e:
            logger.error(f"Broadcast fail for {user_id}: {e}"); fail += 1

    context.user_data['sent_messages'] = all_sent_messages
    query.message.reply_text(f"📢 Done! Sent to: {success}, Failed for: {fail}.\n\nAuto-delete these messages?", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑️ Yes", callback_data="delete_yes"), InlineKeyboardButton("No", callback_data="delete_no")]])); 
    return DELETE_OPTION

def handle_delete_option(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer()
    if query.data == "delete_no": 
        query.edit_message_text("✅ OK. Messages will not be deleted."); 
        context.user_data.clear(); return ConversationHandler.END
    else: 
        query.edit_message_text("Enter deletion delay in minutes (e.g., 10)."); return GET_DELETE_TIME

def get_delete_time(update: Update, context: CallbackContext):
    try: minutes = int(update.message.text); assert minutes > 0
    except: update.message.reply_text("Invalid input."); return GET_DELETE_TIME
    seconds = minutes * 60; sent_messages = context.user_data.get('sent_messages', [])
    for msg in sent_messages: context.job_queue.run_once(delete_messages_job, seconds, context={'chat_id': msg['chat_id'], 'message_ids': [msg['message_id']]})
    update.message.reply_text(f"✅ Deletion scheduled for {len(sent_messages)} messages in {minutes} minute(s)."); 
    context.user_data.clear(); return ConversationHandler.END

# --- Set Message Conversations ---
def set_forcesub_start(update: Update, context: CallbackContext):
    context.user_data['conversation_active'] = True
    update.message.reply_text("Please send the photo for the force subscribe message.\n(Send /skip to use text only)"); return GET_FS_PHOTO
def get_forcesub_photo(update: Update, context: CallbackContext):
    context.user_data['fs_photo_id'] = update.message.photo[-1].file_id if update.message.photo else None
    update.message.reply_text("Photo received. Now, send the text/caption.\n(Send /skip for no text)"); return GET_FS_TEXT
def skip_forcesub_photo(update: Update, context: CallbackContext):
    context.user_data['fs_photo_id'] = None; update.message.reply_text("Photo skipped. Now, please send the text/caption."); return GET_FS_TEXT
def get_forcesub_text(update: Update, context: CallbackContext):
    BOT_CONFIG['force_sub_photo_id'] = context.user_data.get('fs_photo_id')
    BOT_CONFIG['force_sub_text'] = update.message.text; save_bot_config()
    update.message.reply_text("✅ Force subscribe message updated!"); 
    context.user_data.clear(); return ConversationHandler.END
def skip_forcesub_text(update: Update, context: CallbackContext):
    BOT_CONFIG['force_sub_photo_id'] = context.user_data.get('fs_photo_id')
    BOT_CONFIG['force_sub_text'] = None; save_bot_config()
    update.message.reply_text("✅ Force subscribe message updated with photo only!"); 
    context.user_data.clear(); return ConversationHandler.END

def set_start_message_start(update: Update, context: CallbackContext):
    context.user_data['conversation_active'] = True
    update.message.reply_text("Please send the photo for the returning user welcome message.\n(Send /skip for text only)"); return GET_START_PHOTO
def get_start_photo(update: Update, context: CallbackContext):
    context.user_data['start_photo_id'] = update.message.photo[-1].file_id if update.message.photo else None
    update.message.reply_text("Photo received. Now, send the welcome text/caption.\n(Send /skip for no text)"); return GET_START_TEXT
def skip_start_photo(update: Update, context: CallbackContext):
    context.user_data['start_photo_id'] = None; update.message.reply_text("Photo skipped. Now, send the welcome text."); return GET_START_TEXT
def get_start_text(update: Update, context: CallbackContext):
    BOT_CONFIG['welcome_photo_id'] = context.user_data.get('start_photo_id')
    BOT_CONFIG['welcome_text'] = update.message.text; save_bot_config()
    update.message.reply_text("✅ Welcome message updated!"); 
    context.user_data.clear(); return ConversationHandler.END
def skip_start_text(update: Update, context: CallbackContext):
    BOT_CONFIG['welcome_photo_id'] = context.user_data.get('start_photo_id')
    BOT_CONFIG['welcome_text'] = None; save_bot_config()
    update.message.reply_text("✅ Welcome message updated with photo only!"); 
    context.user_data.clear(); return ConversationHandler.END

# --- Admin Commands ---
def stats(update: Update, context: CallbackContext): 
    update.message.reply_text(f"📊 Total Users: {len(get_all_user_ids())}")

def forcesub_on(update: Update, context: CallbackContext): global FORCE_SUBSCRIBE_ENABLED; FORCE_SUBSCRIBE_ENABLED = True; update.message.reply_text("✅ Force Subscribe is ON.")
def forcesub_off(update: Update, context: CallbackContext): global FORCE_SUBSCRIBE_ENABLED; FORCE_SUBSCRIBE_ENABLED = False; update.message.reply_text("❌ Force Subscribe is OFF.")
def protect_on_command(update: Update, context: CallbackContext): global PROTECT_CONTENT_ENABLED; PROTECT_CONTENT_ENABLED = True; update.message.reply_text("✅ Content protection is ON.")
def protect_off_command(update: Update, context: CallbackContext): global PROTECT_CONTENT_ENABLED; PROTECT_CONTENT_ENABLED = False; update.message.reply_text("❌ Content protection is OFF.")

# --- Main Execution ---
def main():
    setup_database(); load_catalog(); load_bot_config()
    updater = Updater(TOKEN, use_context=True, arbitrary_callback_data=True)
    dp = updater.dispatcher; admin_filters = Filters.user(ADMIN_IDS)

    # --- Conversation Handlers (Priority Order) ---
    broadcast_conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_start, filters=admin_filters)],
        states={
            GET_BROADCAST_FILES: [MessageHandler(Filters.all & ~Filters.command, get_broadcast_files), CommandHandler('done', finish_broadcast_files, filters=admin_filters)],
            GET_BROADCAST_TEXT: [MessageHandler(Filters.text & ~Filters.command, get_broadcast_text), CommandHandler('skip', skip_broadcast_text, filters=admin_filters)],
            BROADCAST_CONFIRM: [CallbackQueryHandler(send_broadcast_now, pattern="^send_broadcast_now$")],
            DELETE_OPTION: [CallbackQueryHandler(handle_delete_option, pattern=r"^delete_")],
            GET_DELETE_TIME: [MessageHandler(Filters.text & ~Filters.command, get_delete_time)],
        },
        fallbacks=[CommandHandler('cancel', cancel_conversation, filters=admin_filters), CallbackQueryHandler(cancel_conversation, pattern="^cancel_broadcast$")], conversation_timeout=600)

    set_forcesub_conv = ConversationHandler(
        entry_points=[CommandHandler("setforcesub", set_forcesub_start, filters=admin_filters)],
        states={ 
            GET_FS_PHOTO: [MessageHandler(Filters.photo, get_forcesub_photo), CommandHandler('skip', skip_forcesub_photo, filters=admin_filters)], 
            GET_FS_TEXT: [MessageHandler(Filters.text & ~Filters.command, get_forcesub_text), CommandHandler('skip', skip_forcesub_text, filters=admin_filters)] 
        },
        fallbacks=[CommandHandler('cancel', cancel_conversation, filters=admin_filters)])

    set_start_conv = ConversationHandler(
        entry_points=[CommandHandler("setstart", set_start_message_start, filters=admin_filters)],
        states={ 
            GET_START_PHOTO: [MessageHandler(Filters.photo, get_start_photo), CommandHandler('skip', skip_start_photo, filters=admin_filters)], 
            GET_START_TEXT: [MessageHandler(Filters.text & ~Filters.command, get_start_text), CommandHandler('skip', skip_start_text, filters=admin_filters)] 
        },
        fallbacks=[CommandHandler('cancel', cancel_conversation, filters=admin_filters)])
    
    add_product_conv = ConversationHandler(
        entry_points=[MessageHandler((Filters.document | Filters.video | Filters.photo) & admin_filters, add_product_start)],
        states={ 
            GET_PRODUCT_FILES: [MessageHandler((Filters.document | Filters.video | Filters.photo) & ~Filters.command, get_product_files), CommandHandler('done', finish_adding_files, filters=admin_filters)],
            PRICE: [MessageHandler(Filters.text & ~Filters.command, get_price)] 
        },
        fallbacks=[CommandHandler('cancel', cancel_conversation, filters=admin_filters)])
    
    dp.add_handler(broadcast_conv)
    dp.add_handler(set_forcesub_conv)
    dp.add_handler(set_start_conv)
    dp.add_handler(add_product_conv)

    # --- Command Handlers ---
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("stats", stats, filters=admin_filters))
    dp.add_handler(CommandHandler("forcesub_on", forcesub_on, filters=admin_filters)); dp.add_handler(CommandHandler("forcesub_off", forcesub_off, filters=admin_filters))
    dp.add_handler(CommandHandler("protect_on", protect_on_command, filters=admin_filters)); dp.add_handler(CommandHandler("protect_off", protect_off_command, filters=admin_filters))

    # --- Callback Handlers ---
    dp.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))

    logger.info("🤖 Bot is running..."); updater.start_polling(); updater.idle()

if __name__ == "__main__": main()
