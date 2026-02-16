import os
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List
import pytz
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

# Load environment variables
load_dotenv()

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
WAITING_FOR_ADD_GROUPS, WAITING_FOR_DEL_GROUPS, WAITING_FOR_TASK_MESSAGE, WAITING_FOR_DEL_TASKS, WAITING_FOR_DEL_USERS, WAITING_FOR_ADD_USERS = range(6)

# File paths - Use script directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GROUPS_FILE = os.path.join(SCRIPT_DIR, 'groups.json')
TASKS_FILE = os.path.join(SCRIPT_DIR, 'tasks.json')
ALLOWED_USERS_FILE = os.path.join(SCRIPT_DIR, 'allowed_users.json')
USER_ACTIONS_LOG = os.path.join(SCRIPT_DIR, 'user_actions.log')

# Timezone - Load from environment or default to UTC
TIMEZONE = pytz.timezone(os.getenv('TIMEZONE', 'UTC'))

# Setup action logger for user activities
action_logger = logging.getLogger('user_actions')
action_logger.setLevel(logging.INFO)
action_handler = logging.FileHandler(USER_ACTIONS_LOG)
action_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
action_logger.addHandler(action_handler)


class ReminderBot:
    def __init__(self, token: str):
        self.token = token
        # Configure scheduler with misfire grace time (allow tasks to run if late by up to 5 minutes)
        self.scheduler = AsyncIOScheduler(
            timezone=TIMEZONE,
            job_defaults={'misfire_grace_time': 300}  # 5 minutes in seconds
        )
        self.temp_task_data = {}
        self.pagination_data = {}  # Store pagination state
        self.active_conversations = {}  # Track active conversation per user
        
    def load_json(self, filename: str) -> dict:
        """Load data from JSON file"""
        try:
            with open(filename, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
    
    def save_json(self, filename: str, data: dict):
        """Save data to JSON file"""
        with open(filename, 'w') as f:
            json.dump(data, f, indent=2)
    
    def format_date_display(self, date_str: str) -> str:
        """Convert date from YYYY-MM-DD to DD-MM-YYYY for display"""
        try:
            parts = date_str.split('-')
            if len(parts) == 3:
                return f"{parts[2]}-{parts[1]}-{parts[0]}"
            return date_str
        except:
            return date_str
    
    def escape_markdown(self, text: str) -> str:
        """Escape special characters for Telegram MarkdownV1"""
        if not text:
            return text
        # Characters that need escaping in Telegram MarkdownV1
        special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        for char in special_chars:
            text = text.replace(char, f'\\{char}')
        return text
    
    async def get_user_name(self, user_id: int, context: ContextTypes.DEFAULT_TYPE = None) -> str:
        """Get user's display name from allowed users list or fetch from Telegram"""
        allowed_data = self.load_json(ALLOWED_USERS_FILE)
        allowed_list = allowed_data.get('allowed_users', [])
        
        for user in allowed_list:
            # Support both old (int) and new (dict) format
            if isinstance(user, dict):
                if user.get('user_id') == user_id:
                    return user.get('name', f'User {user_id}')
            elif isinstance(user, int) and user == user_id:
                # Try to fetch from Telegram if context is available
                if context:
                    try:
                        chat = await context.bot.get_chat(user_id)
                        name = chat.first_name or "Unknown"
                        if chat.last_name:
                            name += f" {chat.last_name}"
                        return name
                    except:
                        pass
                return f'User {user_id}'
        
        # Try to fetch from Telegram as last resort
        if context:
            try:
                chat = await context.bot.get_chat(user_id)
                name = chat.first_name or "Unknown"
                if chat.last_name:
                    name += f" {chat.last_name}"
                return name
            except:
                pass
        
        return f'User {user_id}'
    
    def log_user_action(self, user_id: int, username: str, action: str, details: str = ""):
        """Log user actions to file"""
        user_info = f"User {username} (ID: {user_id})"
        log_message = f"{user_info} - {action}"
        if details:
            log_message += f" - {details}"
        action_logger.info(log_message)
    
    def cleanup_tasks(self):
        """Remove completed tasks and renumber remaining tasks serially"""
        tasks = self.load_json(TASKS_FILE)
        task_list = tasks.get('tasks', [])
        
        # Filter out completed tasks
        active_tasks = [task for task in task_list if not task.get('completed', False)]
        
        # Renumber tasks serially
        for idx, task in enumerate(active_tasks, 1):
            task['id'] = idx
        
        # Save cleaned up tasks
        tasks['tasks'] = active_tasks
        self.save_json(TASKS_FILE, tasks)
        
        return len(active_tasks)
    
    def get_admin_list(self) -> list:
        """Get complete admin list including super admin"""
        admin_list = []
        
        # Get super admin
        super_admin_id = os.getenv('SUPER_ADMIN_ID')
        if super_admin_id:
            admin_list.append(int(super_admin_id))
        
        # Get regular admins
        admin_ids = os.getenv('ADMIN_USER_ID', '')
        if admin_ids:
            for uid in admin_ids.split(','):
                uid = uid.strip()
                if uid:
                    uid_int = int(uid)
                    if uid_int not in admin_list:  # Avoid duplicates
                        admin_list.append(uid_int)
        
        return admin_list
    
    def is_admin(self, user_id: int) -> bool:
        """Check if user is admin or super admin (from .env file)"""
        return user_id in self.get_admin_list()
    
    def is_authorized(self, user_id: int) -> bool:
        """Check if user is authorized"""
        allowed_data = self.load_json(ALLOWED_USERS_FILE)
        allowed_list = allowed_data.get('allowed_users', [])
        
        for user in allowed_list:
            # Support both old (int) and new (dict) format
            if isinstance(user, dict) and user.get('user_id') == user_id:
                return True
            elif isinstance(user, int) and user == user_id:
                return True
        
        return False
    
    async def check_and_cancel_previous_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE, new_conversation: str) -> bool:
        """Check if user has active conversation and notify about cancellation"""
        user_id = update.effective_user.id
        
        if user_id in self.active_conversations:
            previous = self.active_conversations[user_id]
            if previous != new_conversation:
                # Clean up temp data if exists
                if user_id in self.temp_task_data:
                    del self.temp_task_data[user_id]
                
                # Notify user about session cancellation with previous session name
                await update.message.reply_text(
                    f"⚠️ <b>Previous '{previous}' session cancelled</b>\n"
                    f"Starting '{new_conversation}' session...",
                    parse_mode='HTML'
                )
        
        # Track new conversation
        self.active_conversations[user_id] = new_conversation
        return True
    
    async def clear_conversation(self, user_id: int):
        """Clear user's active conversation"""
        if user_id in self.active_conversations:
            del self.active_conversations[user_id]
    
    async def cancel_active_session_if_exists(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Cancel any active session and notify user. Returns True if session was cancelled."""
        user_id = update.effective_user.id
        
        if user_id in self.active_conversations:
            session_name = self.active_conversations[user_id]
            del self.active_conversations[user_id]
            
            # Clean up temp data if exists
            if user_id in self.temp_task_data:
                del self.temp_task_data[user_id]
            
            await update.message.reply_text(
                f"⚠️ <b>Previous '{session_name}' session cancelled</b>",
                parse_mode='HTML'
            )
            return True
        return False
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start command handler"""
        # Cancel any active session
        await self.cancel_active_session_if_exists(update, context)
        
        user = update.effective_user
        welcome_text = f"""
🤖 Welcome to Task Reminder Bot, {user.first_name}!

<b>━━━━━━ Manage Task ━━━━━━</b>

/add_task - Create a new task
/del_task - Delete pending task
/task_list - View all tasks



<b>━━━━━━ Manage Groups ━━━━━━</b>

/manage_groups - Manage groups (Add/Delete/List)



<b>━━━━━━ Manage users ━━━━━━</b>

<b>ADMIN ONLY 🦁</b>

/manage_users - Manage users (Add/Remove/List)


/help - Show this help message
        """
        await update.message.reply_text(welcome_text, parse_mode='HTML')
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show help message"""
        # Cancel any active session
        await self.cancel_active_session_if_exists(update, context)
        
        help_text = """📋 Available Commands:

<b>━━━━━━ Manage Task ━━━━━━</b>

/add_task - Create a new task
/del_task - Delete pending task
/task_list - View all tasks



<b>━━━━━━ Manage Groups ━━━━━━</b>

/manage_groups - Manage groups (Add/Delete/List)



<b>━━━━━━ Manage users ━━━━━━</b>

<b>ADMIN ONLY 🦁</b>

/manage_users - Manage users (Add/Remove/List)

"""
        await update.message.reply_text(help_text, parse_mode='HTML')
    
    async def manage_groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show group management menu"""
        if not self.is_authorized(update.effective_user.id):
            await update.message.reply_text("You are not authorized to use this.")
            return
        
        # Cancel any active session
        await self.cancel_active_session_if_exists(update, context)
        
        keyboard = [
            [InlineKeyboardButton("➕ Add Groups", callback_data="menu_add_group")],
            [InlineKeyboardButton("🗑️ Delete Groups", callback_data="menu_del_group")],
            [InlineKeyboardButton("📑 List All Groups", callback_data="menu_list_groups")],
        ]
        
        await update.message.reply_text(
            "👥 <b>Group Management</b>\n\n"
            "Select an action:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    
    async def manage_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show user management menu (Admin only)"""
        if not self.is_admin(update.effective_user.id):
            await update.message.reply_text("You are not authorized to use this.")
            return
        
        # Cancel any active session
        await self.cancel_active_session_if_exists(update, context)
        
        keyboard = [
            [InlineKeyboardButton("➕ Add User", callback_data="menu_add_user")],
            [InlineKeyboardButton("🗑️ Remove User", callback_data="menu_remove_user")],
            [InlineKeyboardButton("📑 List All Users", callback_data="menu_list_users")],
        ]
        
        await update.message.reply_text(
            "👤 <b>User Management</b>\n\n"
            "Select an action:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    
    async def add_group(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start interactive add group session"""
        if not self.is_authorized(update.effective_user.id):
            await update.message.reply_text("You are not authorized to use this.")
            return ConversationHandler.END
        
        # Check and notify about previous session
        await self.check_and_cancel_previous_session(update, context, 'add_group')
        
        await update.message.reply_text(
            "📝 **Add Groups - Interactive Mode**\n\n"
            "Send group ID one per line like below:\n"
            "`-1001234567891`\n"
            "`-1009876543142`\n\n"
            "Bot will automatically fetch group names from Telegram!\n\n"
            "• Type /done when finished\n"
            "• Session auto-closes in 5 minutes\n\n",
            parse_mode='Markdown'
        )
        return WAITING_FOR_ADD_GROUPS
    
    async def forward_to_active_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
        """Forward update to the currently active session's handler"""
        if user_id not in self.active_conversations:
            return
        
        session = self.active_conversations[user_id]
        handler_map = {
            'add_group': self.receive_add_groups,
            'del_group': self.receive_del_groups,
            'add_task': self.receive_task_message,
            'del_task': self.receive_del_tasks,
            'add_user': self.receive_add_users,
            'remove_user': self.receive_del_users
        }
        
        if session in handler_map:
            await handler_map[session](update, context)

    async def receive_add_groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive and add groups"""
        user_id = update.effective_user.id
        
        # Check if this session is still active (not replaced by another command)
        if user_id in self.active_conversations and self.active_conversations[user_id] != 'add_group':
            # Forward to the correct handler and end this conversation
            await self.forward_to_active_session(update, context, user_id)
            return ConversationHandler.END
        
        text = update.message.text.strip()
        
        groups_data = self.load_json(GROUPS_FILE)
        if 'groups' not in groups_data:
            groups_data['groups'] = {}
        
        lines = text.split('\n')
        added = []
        already_exists = []
        invalid = []
        failed = []
        
        for line in lines:
            group_id = line.strip()
            
            # Skip empty lines
            if not group_id:
                continue
            
            # Validate group ID starts with minus
            if not group_id.startswith('-'):
                invalid.append(f"{group_id} (must start with -)")
                continue
            
            # Check if group already exists
            if group_id in groups_data['groups']:
                already_exists.append(f"{self.escape_markdown(groups_data['groups'][group_id])} ({self.escape_markdown(group_id)})")
                continue
            
            # Try to fetch group name from Telegram
            try:
                chat = await context.bot.get_chat(chat_id=group_id)
                group_name = chat.title
                groups_data['groups'][group_id] = group_name
                # Store unescaped for later display, escape for Markdown
                added.append(f"{self.escape_markdown(group_name)} ({self.escape_markdown(group_id)})")
            except Exception as e:
                failed.append(f"{self.escape_markdown(group_id)} (can't access group - make sure bot is added)")
                logger.error(f"Failed to get chat {group_id}: {e}")
        
        response = ""
        if added:
            self.save_json(GROUPS_FILE, groups_data)
            
            # Log action (no escaping needed for log)
            username = update.effective_user.username or update.effective_user.first_name or "Unknown"
            # Create unescaped version for logging
            added_for_log = []
            for line in lines:
                group_id = line.strip()
                if group_id in groups_data['groups']:
                    added_for_log.append(f"{groups_data['groups'][group_id]} ({group_id})")
            self.log_user_action(
                update.effective_user.id,
                username,
                "GROUP_ADDED",
                f"Added {len(added)} group(s): {', '.join(added_for_log)}"
            )
            
            response += f"✅ **Added {len(added)} group(s):**\n" + "\n".join([f"• {g}" for g in added]) + "\n\n"
        
        if already_exists:
            response += f"⚠️ **Already registered:**\n" + "\n".join([f"• {g}" for g in already_exists]) + "\n\n"
        
        if invalid:
            response += f"❌ **Invalid group IDs:**\n" + "\n".join([f"• {inv}" for inv in invalid]) + "\n\n"
        
        if failed:
            response += f"⚠️ **Failed to access:**\n" + "\n".join([f"• {f}" for f in failed]) + "\n\n"
        
        if not added and not already_exists and not invalid and not failed:
            response = "❌ Invalid format. Send group ID: `-1001234567891`\n\n"
        
        response += "Send more group IDs or /done to finish."
        
        await update.message.reply_text(response, parse_mode='Markdown')
        
        return WAITING_FOR_ADD_GROUPS
    
    async def del_group(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start interactive delete group session"""
        if not self.is_authorized(update.effective_user.id):
            await update.message.reply_text("You're not authorized to use this command.")
            return ConversationHandler.END
        
        # Check and notify about previous session
        await self.check_and_cancel_previous_session(update, context, 'del_group')
        
        groups_data = self.load_json(GROUPS_FILE)
        groups = groups_data.get('groups', {})
        
        if not groups:
            await self.clear_conversation(update.effective_user.id)
            await update.message.reply_text("📭 No groups to delete.", parse_mode='Markdown')
            return ConversationHandler.END
        
        group_list = "\n".join([f"{idx}. {self.escape_markdown(name)}: {self.escape_markdown(gid)}" for idx, (gid, name) in enumerate(groups.items(), 1)])
        
        await update.message.reply_text(
            f"🗑️ **Delete Groups - Interactive Mode**\n\n"
            f"**Current Groups:**\n{group_list}\n\n"
            "Send serial number(s) to delete (space-separated):\n"
            "Example: 1 3 5\n\n"
            "• Type /done when finished\n"
            "• Session auto-closes in 5 minutes",
            parse_mode='Markdown'
        )
        return WAITING_FOR_DEL_GROUPS
    
    async def receive_del_groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive and delete groups"""
        user_id = update.effective_user.id
        
        # Check if this session is still active (not replaced by another command)
        if user_id in self.active_conversations and self.active_conversations[user_id] != 'del_group':
            # Forward to the correct handler and end this conversation
            await self.forward_to_active_session(update, context, user_id)
            return ConversationHandler.END
        
        text = update.message.text.strip()
        
        groups_data = self.load_json(GROUPS_FILE)
        if 'groups' not in groups_data:
            groups_data['groups'] = {}
        
        # Parse serial numbers (space or newline separated)
        try:
            serial_numbers = [int(num.strip()) for num in text.replace('\n', ' ').split()]
        except ValueError:
            await update.message.reply_text("❌ Invalid input. Please provide serial numbers only (e.g., 1 2 3)")
            return WAITING_FOR_DEL_GROUPS
        
        # Convert groups dict to list for indexing
        group_items = list(groups_data['groups'].items())
        deleted = []
        not_found = []
        
        for serial in serial_numbers:
            if 1 <= serial <= len(group_items):
                group_id, group_name = group_items[serial - 1]
                del groups_data['groups'][group_id]
                deleted.append(f"{self.escape_markdown(group_name)} ({self.escape_markdown(group_id)})")
            else:
                not_found.append(str(serial))
        
        if deleted:
            self.save_json(GROUPS_FILE, groups_data)
            
            # Log action
            username = update.effective_user.username or update.effective_user.first_name or "Unknown"
            self.log_user_action(
                update.effective_user.id,
                username,
                "GROUP_DELETED",
                f"Deleted {len(deleted)} group(s): {', '.join(deleted)}"
            )
        
        response = ""
        if deleted:
            response += f"✅ **Deleted {len(deleted)} group(s):**\n" + "\n".join([f"• {g}" for g in deleted]) + "\n"
        if not_found:
            response += f"\n⚠️ **Serial number(s) not found:** {', '.join(not_found)}\n"
        
        response += "\nSend more serial numbers or /done to finish."
        
        await update.message.reply_text(response, parse_mode='Markdown')
        return WAITING_FOR_DEL_GROUPS
    
    async def list_groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List all registered groups"""
        if not self.is_authorized(update.effective_user.id):
            await update.message.reply_text("You are not authorized to use this.")
            return
        
        # Cancel any active session
        await self.cancel_active_session_if_exists(update, context)
        
        # Log action
        username = update.effective_user.username or update.effective_user.first_name or "Unknown"
        self.log_user_action(update.effective_user.id, username, "VIEWED_GROUP_LIST", "")
        
        groups_data = self.load_json(GROUPS_FILE)
        groups = groups_data.get('groups', {})
        
        if not groups:
            await update.message.reply_text("📭 No groups registered yet.\n\nUse /add_group to add groups.")
            return
        
        message = "👥 **Registered Groups:**\n\n"
        for idx, (group_id, group_name) in enumerate(groups.items(), 1):
            message += f"{idx}. **{self.escape_markdown(group_name)}**: {self.escape_markdown(group_id)}\n\n"
        
        message += f"**Total:** {len(groups)} group(s)"
        
        await update.message.reply_text(message, parse_mode='Markdown')
    
    async def add_task_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start add task conversation"""
        if not self.is_authorized(update.effective_user.id):
            await update.message.reply_text("You are not authorized to use this.")
            return ConversationHandler.END
        
        # Check and notify about previous session
        await self.check_and_cancel_previous_session(update, context, 'add_task')
        
        user_id = update.effective_user.id
        self.temp_task_data[user_id] = {}
        
        await update.message.reply_text(
            "Please enter the task message:\n\n"
            "Type /done to cancel",
            parse_mode='Markdown'
        )
        return WAITING_FOR_TASK_MESSAGE
    
    async def receive_task_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive task message"""
        user_id = update.effective_user.id
        
        # Check if this session is still active (not replaced by another command)
        if user_id in self.active_conversations and self.active_conversations[user_id] != 'add_task':
            # Forward to the correct handler and end this conversation
            await self.forward_to_active_session(update, context, user_id)
            return ConversationHandler.END
        
        text = update.message.text.strip()
        
        # Handle custom minute input
        if 'pending_hour' in self.temp_task_data[user_id]:
            try:
                minute = int(text)
                if 0 <= minute <= 59:
                    hour = self.temp_task_data[user_id]['pending_hour']
                    time_str = f"{hour:02d}:{minute:02d}"
                    self.temp_task_data[user_id]['time'] = time_str
                    del self.temp_task_data[user_id]['pending_hour']
                    
                    if 'selected_groups' not in self.temp_task_data[user_id]:
                        self.temp_task_data[user_id]['selected_groups'] = []
                    
                    keyboard = self.generate_group_keyboard()
                    await update.message.reply_text(
                        f"✅ Date: {self.format_date_display(self.temp_task_data[user_id].get('date'))}\n"
                        f"✅ Time: {time_str}\n\n**Select Group(s):**",
                        reply_markup=keyboard,
                        parse_mode='Markdown'
                    )
                    return WAITING_FOR_TASK_MESSAGE
                else:
                    await update.message.reply_text("❌ Invalid minute. Please enter a number between 0 and 59:")
                    return WAITING_FOR_TASK_MESSAGE
            except ValueError:
                await update.message.reply_text("❌ Invalid input. Please enter a number between 0 and 59:")
                return WAITING_FOR_TASK_MESSAGE
        
        self.temp_task_data[user_id]['message'] = text
        
        keyboard = self.generate_calendar()
        await update.message.reply_text(
            "📅 **Select Date:**",
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
        return WAITING_FOR_TASK_MESSAGE
    
    def generate_calendar(self, year: int = None, month: int = None) -> InlineKeyboardMarkup:
        """Generate calendar keyboard"""
        now = datetime.now(TIMEZONE)
        if year is None:
            year = now.year
        if month is None:
            month = now.month
        
        # Month names
        month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 
                       'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
        
        # Calendar header
        keyboard = [[
            InlineKeyboardButton("◀️", callback_data=f"cal_prev_{year}_{month}"),
            InlineKeyboardButton(f"{month_names[month-1]} {year}", callback_data="cal_ignore"),
            InlineKeyboardButton("▶️", callback_data=f"cal_next_{year}_{month}")
        ]]
        
        # Weekday headers
        keyboard.append([InlineKeyboardButton(day, callback_data="cal_ignore") 
                        for day in ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su']])
        
        # Get first day of month and number of days
        import calendar
        month_calendar = calendar.monthcalendar(year, month)
        
        for week in month_calendar:
            row = []
            for day in week:
                if day == 0:
                    row.append(InlineKeyboardButton(" ", callback_data="cal_ignore"))
                else:
                    date_str = f"{year}-{month:02d}-{day:02d}"
                    # Check if date is in the past
                    check_date = TIMEZONE.localize(datetime(year, month, day))
                    if check_date.date() < now.date():
                        row.append(InlineKeyboardButton(str(day), callback_data="cal_ignore"))
                    else:
                        row.append(InlineKeyboardButton(str(day), callback_data=f"date_{date_str}"))
            keyboard.append(row)
        
        return InlineKeyboardMarkup(keyboard)
    
    def generate_time_keyboard(self) -> InlineKeyboardMarkup:
        """Generate time selection keyboard"""
        keyboard = []
        
        # Header with timezone info
        keyboard.append([InlineKeyboardButton("⏰ Select Hour (GMT+6):", callback_data="time_ignore")])
        
        # Morning + Afternoon (6 AM - 5 PM)
        keyboard.append([InlineKeyboardButton("🌅 Morning & Afternoon (06:00 - 17:00)", callback_data="time_ignore")])
        # Morning hours (6 AM - 11 AM)
        row = [InlineKeyboardButton(f"{h:02d}:00", callback_data=f"hour_{h}") 
               for h in range(6, 12)]
        keyboard.append(row)
        # Afternoon hours (12 PM - 5 PM)
        row = [InlineKeyboardButton(f"{h:02d}:00", callback_data=f"hour_{h}") 
               for h in range(12, 18)]
        keyboard.append(row)
        
        # Evening + Night (6 PM - 5 AM)
        keyboard.append([InlineKeyboardButton("🌆 Evening & Night (18:00 - 05:00)", callback_data="time_ignore")])
        # Evening hours (6 PM - 11 PM)
        row = [InlineKeyboardButton(f"{h:02d}:00", callback_data=f"hour_{h}") 
               for h in range(18, 24)]
        keyboard.append(row)
        # Night hours (12 AM - 5 AM)
        row = [InlineKeyboardButton(f"{h:02d}:00", callback_data=f"hour_{h}") 
               for h in range(0, 6)]
        keyboard.append(row)
        
        return InlineKeyboardMarkup(keyboard)
    
    def generate_minute_keyboard(self, hour: int) -> InlineKeyboardMarkup:
        """Generate minute selection keyboard"""
        keyboard = []
        keyboard.append([InlineKeyboardButton(f"⏰ Select Minute (Hour: {hour:02d}):", callback_data="time_ignore")])
        
        # Generate 15-minute intervals
        minutes = [0, 15, 30, 45]
        row = [InlineKeyboardButton(f":{m:02d}", callback_data=f"minute_{hour}_{m}") 
               for m in minutes]
        keyboard.append(row)
        
        # Add custom minute option
        keyboard.append([InlineKeyboardButton("🔢 Custom Minute", callback_data=f"custom_minute_{hour}")])
        
        return InlineKeyboardMarkup(keyboard)
    
    def generate_group_keyboard(self) -> InlineKeyboardMarkup:
        """Generate group selection keyboard"""
        groups_data = self.load_json(GROUPS_FILE)
        groups = groups_data.get('groups', {})
        
        keyboard = []
        
        # Add "Myself" option at the top for personal reminders
        keyboard.append([InlineKeyboardButton(
            f"☐ Myself", 
            callback_data=f"group_myself"
        )])
        
        for group_id, group_name in groups.items():
            keyboard.append([InlineKeyboardButton(
                f"☐ {group_name}", 
                callback_data=f"group_{group_id}"
            )])
        
        keyboard.append([InlineKeyboardButton("✅ Done Selecting", callback_data="group_done")])
        
        return InlineKeyboardMarkup(keyboard)
    
    def generate_frequency_keyboard(self) -> InlineKeyboardMarkup:
        """Generate frequency selection keyboard"""
        keyboard = [
            [
                InlineKeyboardButton("🔂 Once", callback_data="freq_once"),
                InlineKeyboardButton("🔁 Daily", callback_data="freq_daily")
            ],
            [
                InlineKeyboardButton("📅 Weekly", callback_data="freq_weekly"),
                InlineKeyboardButton("📆 Monthly", callback_data="freq_monthly")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle all callback queries"""
        query = update.callback_query
        await query.answer()
        
        user_id = update.effective_user.id
        data = query.data
        
        # Handle pagination callbacks
        if data.startswith('page_'):
            await self.handle_pagination(update, context)
            return
        
        # Handle delete task pagination callbacks
        if data.startswith('delpage_'):
            await self.handle_delete_pagination(update, context)
            return
        
        # Handle acknowledgment button
        if data.startswith('ack_'):
            user_first_name = update.effective_user.first_name
            try:
                # Update button text to show thanks
                keyboard = [[InlineKeyboardButton(f"Thanks {user_first_name}", callback_data="ack_done")]]
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
            except Exception as e:
                logger.error(f"Failed to update acknowledgment button: {e}")
            return
        
        # Ignore already acknowledged button
        if data == 'ack_done':
            await query.answer("Already acknowledged!", show_alert=False)
            return
        
        # Handle menu list callbacks (no session validation needed)
        # Conversation-starting menu callbacks are handled by their respective conversation handlers
        if data.startswith('menu_list_'):
            chat_id = query.message.chat_id
            
            # Helper class to wrap chat for callback handlers
            class FakeMessage:
                def __init__(self, chat_id, bot):
                    self.chat = type('obj', (object,), {'id': chat_id})()
                    self.chat_id = chat_id
                    self._bot = bot
                async def reply_text(self, *args, **kwargs):
                    return await self._bot.send_message(self.chat_id, *args, **kwargs)
            
            if data == 'menu_list_groups':
                await query.message.delete()
                fake_update = type('obj', (object,), {
                    'message': FakeMessage(chat_id, context.bot),
                    'effective_user': update.effective_user
                })()
                await self.list_groups(fake_update, context)
                return
            elif data == 'menu_list_users':
                await query.message.delete()
                fake_update = type('obj', (object,), {
                    'message': FakeMessage(chat_id, context.bot),
                    'effective_user': update.effective_user
                })()
                await self.list_users(fake_update, context)
            return
        
        # Handle conversation-starting menu callbacks
        # These are processed by conversation handlers, but we handle the initial message here
        if data in ['menu_add_group', 'menu_del_group', 'menu_add_user', 'menu_remove_user']:
            chat_id = query.message.chat_id
            await query.message.delete()
            
            if data == 'menu_add_group':
                if not self.is_authorized(user_id):
                    await context.bot.send_message(chat_id, "You are not authorized to use this.")
                    return ConversationHandler.END
                
                # Check and notify about previous session
                if user_id in self.active_conversations:
                    previous = self.active_conversations[user_id]
                    if previous != 'add_group':
                        # Clean up temp data if exists
                        if user_id in self.temp_task_data:
                            del self.temp_task_data[user_id]
                        
                        await context.bot.send_message(
                            chat_id,
                            f"⚠️ <b>Previous '{previous}' session cancelled</b>\n"
                            f"Starting 'add_group' session...",
                            parse_mode='HTML'
                        )
                
                # Track new conversation
                self.active_conversations[user_id] = 'add_group'
                
                await context.bot.send_message(
                    chat_id,
                    "📝 **Add Groups - Interactive Mode**\n\n"
                    "Send group ID one per line like below:\n"
                    "`-1001234567891`\n"
                    "`-1009876543142`\n\n"
                    "Bot will automatically fetch group names from Telegram!\n\n"
                    "• Type /done when finished\n"
                    "• Session auto-closes in 5 minutes",
                    parse_mode='Markdown'
                )
                return WAITING_FOR_ADD_GROUPS
            elif data == 'menu_del_group':
                # Create fake message for handler
                class FakeMessage:
                    def __init__(self, chat_id, bot):
                        self.chat = type('obj', (object,), {'id': chat_id})()
                        self.chat_id = chat_id
                        self._bot = bot
                    async def reply_text(self, *args, **kwargs):
                        return await self._bot.send_message(self.chat_id, *args, **kwargs)
                
                fake_update = type('obj', (object,), {
                    'message': FakeMessage(chat_id, context.bot),
                    'effective_user': update.effective_user
                })()
                return await self.del_group(fake_update, context)
            elif data == 'menu_add_user':
                class FakeMessage:
                    def __init__(self, chat_id, bot):
                        self.chat = type('obj', (object,), {'id': chat_id})()
                        self.chat_id = chat_id
                        self._bot = bot
                    async def reply_text(self, *args, **kwargs):
                        return await self._bot.send_message(self.chat_id, *args, **kwargs)
                
                fake_update = type('obj', (object,), {
                    'message': FakeMessage(chat_id, context.bot),
                    'effective_user': update.effective_user
                })()
                return await self.add_user(fake_update, context)
            elif data == 'menu_remove_user':
                class FakeMessage:
                    def __init__(self, chat_id, bot):
                        self.chat = type('obj', (object,), {'id': chat_id})()
                        self.chat_id = chat_id
                        self._bot = bot
                    async def reply_text(self, *args, **kwargs):
                        return await self._bot.send_message(self.chat_id, *args, **kwargs)
                
                fake_update = type('obj', (object,), {
                    'message': FakeMessage(chat_id, context.bot),
                    'effective_user': update.effective_user
                })()
                return await self.remove_user(fake_update, context)
        
        # Validate user has active task creation session
        if user_id not in self.temp_task_data or not self.temp_task_data[user_id]:
            logger.warning(f"Session validation failed for user {user_id}, callback: {data}")
            await query.edit_message_text(
                "❌ This task creation session has expired.\n\n"
                "Please start a new task with /add_task"
            )
            return ConversationHandler.END
        
        # Validate required fields for specific operations
        if data.startswith('hour_') and 'date' not in self.temp_task_data[user_id]:
            await query.edit_message_text("❌ Session expired. Start new task with /add_task")
            return ConversationHandler.END
        
        if data.startswith('minute_') and 'date' not in self.temp_task_data[user_id]:
            await query.edit_message_text("❌ Session expired. Start new task with /add_task")
            return ConversationHandler.END
        
        # Calendar navigation
        if data.startswith('cal_prev_') or data.startswith('cal_next_'):
            parts = data.split('_')
            year, month = int(parts[2]), int(parts[3])
            
            if 'prev' in data:
                month -= 1
                if month == 0:
                    month = 12
                    year -= 1
            else:
                month += 1
                if month == 13:
                    month = 1
                    year += 1
            
            keyboard = self.generate_calendar(year, month)
            await query.edit_message_reply_markup(reply_markup=keyboard)
            return WAITING_FOR_TASK_MESSAGE
        
        # Date selection
        elif data.startswith('date_'):
            date_str = data.split('_', 1)[1]
            self.temp_task_data[user_id]['date'] = date_str
            
            keyboard = self.generate_time_keyboard()
            await query.edit_message_text(
                f"✅ Date: {self.format_date_display(date_str)}\n\n⏰ **Select Time (Hour):**",
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
            return WAITING_FOR_TASK_MESSAGE
        
        # Hour selection
        elif data.startswith('hour_'):
            hour = int(data.split('_')[1])
            keyboard = self.generate_minute_keyboard(hour)
            await query.edit_message_text(
                f"✅ Date: {self.format_date_display(self.temp_task_data[user_id].get('date'))}\n"
                f"⏰ Hour: {hour:02d}\n\n**Select Minute:**",
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
            return WAITING_FOR_TASK_MESSAGE
        
        # Custom minute selection
        elif data.startswith('custom_minute_'):
            hour = int(data.split('_')[2])
            await query.edit_message_text(
                f"✅ Date: {self.format_date_display(self.temp_task_data[user_id].get('date'))}\n"
                f"⏰ Hour: {hour:02d}\n\n🔢 **Enter custom minute (0-59):**\n\n"
                "Type a number from 0 to 59",
                parse_mode='Markdown'
            )
            # Store hour for later use
            self.temp_task_data[user_id]['pending_hour'] = hour
            return WAITING_FOR_TASK_MESSAGE
        
        # Minute selection
        elif data.startswith('minute_'):
            parts = data.split('_')
            hour, minute = int(parts[1]), int(parts[2])
            time_str = f"{hour:02d}:{minute:02d}"
            self.temp_task_data[user_id]['time'] = time_str
            
            if 'selected_groups' not in self.temp_task_data[user_id]:
                self.temp_task_data[user_id]['selected_groups'] = []
            
            keyboard = self.generate_group_keyboard()
            await query.edit_message_text(
                f"✅ Date: {self.format_date_display(self.temp_task_data[user_id].get('date'))}\n"
                f"✅ Time: {time_str}\n\n**Select Group(s):**",
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
            return WAITING_FOR_TASK_MESSAGE
        
        # Group selection
        elif data.startswith('group_') and not data == 'group_done' and not data == 'group_ignore':
            group_id = data.split('_', 1)[1]
            
            if 'selected_groups' not in self.temp_task_data[user_id]:
                self.temp_task_data[user_id]['selected_groups'] = []
            
            selected = self.temp_task_data[user_id]['selected_groups']
            
            if group_id in selected:
                selected.remove(group_id)
            else:
                selected.append(group_id)
            
            # Update keyboard to show selection
            groups_data = self.load_json(GROUPS_FILE)
            groups = groups_data.get('groups', {})
            
            keyboard = []
            
            # Add "Myself" option at the top
            check_myself = "☑" if "myself" in selected else "☐"
            keyboard.append([InlineKeyboardButton(
                f"{check_myself} Myself", 
                callback_data=f"group_myself"
            )])
            
            for gid, gname in groups.items():
                check = "☑" if gid in selected else "☐"
                keyboard.append([InlineKeyboardButton(
                    f"{check} {gname}", 
                    callback_data=f"group_{gid}"
                )])
            
            keyboard.append([InlineKeyboardButton("✅ Done Selecting", callback_data="group_done")])
            
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
            return WAITING_FOR_TASK_MESSAGE
        
        # Group selection done
        elif data == 'group_done':
            if not self.temp_task_data[user_id].get('selected_groups'):
                await query.answer("Please select at least one group!", show_alert=True)
                return WAITING_FOR_TASK_MESSAGE
            
            keyboard = self.generate_frequency_keyboard()
            
            groups_data = self.load_json(GROUPS_FILE)
            groups = groups_data.get('groups', {})
            selected_names = []
            for gid in self.temp_task_data[user_id]['selected_groups']:
                if gid == 'myself':
                    selected_names.append('Myself')
                else:
                    selected_names.append(self.escape_markdown(groups.get(gid, gid)))
            
            await query.edit_message_text(
                f"✅ Date: {self.format_date_display(self.temp_task_data[user_id].get('date'))}\n"
                f"✅ Time: {self.temp_task_data[user_id].get('time')}\n"
                f"✅ Groups: {', '.join(selected_names)}\n\n"
                f"**Select Frequency:**",
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
            return WAITING_FOR_TASK_MESSAGE
        
        # Frequency selection
        elif data.startswith('freq_'):
            logger.info(f"User {user_id} selected frequency: {data}")
            
            # Validate required fields are present
            required_fields = ['message', 'date', 'time', 'selected_groups']
            missing_fields = [field for field in required_fields if field not in self.temp_task_data[user_id]]
            
            if missing_fields:
                logger.error(f"Missing fields for user {user_id}: {missing_fields}")
                await query.edit_message_text(
                    "❌ Session data incomplete. Please start a new task with /add_task"
                )
                return ConversationHandler.END
            
            if 'once' in data:
                frequency = 'once'
            elif 'daily' in data:
                frequency = 'daily'
            elif 'weekly' in data:
                frequency = 'weekly'
            elif 'monthly' in data:
                frequency = 'monthly'
            else:
                frequency = 'once'
            self.temp_task_data[user_id]['frequency'] = frequency
            
            # Save task
            await self.save_task(user_id, query)
            return ConversationHandler.END
        
        # Ignore callbacks
        elif data in ['cal_ignore', 'time_ignore', 'group_ignore']:
            return WAITING_FOR_TASK_MESSAGE
    
    async def save_task(self, user_id: int, query):
        """Save the task and schedule it"""
        task_data = self.temp_task_data[user_id]
        
        # Cleanup completed tasks and renumber
        self.cleanup_tasks()
        
        # Load tasks
        tasks = self.load_json(TASKS_FILE)
        if 'tasks' not in tasks:
            tasks['tasks'] = []
        
        # Create task
        task_id = len(tasks['tasks']) + 1
        task = {
            'id': task_id,
            'message': task_data['message'],
            'date': task_data['date'],
            'time': task_data['time'],
            'groups': task_data['selected_groups'],
            'frequency': task_data['frequency'],
            'created_by': user_id,
            'created_at': datetime.now(TIMEZONE).isoformat()
        }
        
        tasks['tasks'].append(task)
        self.save_json(TASKS_FILE, tasks)
        
        # Schedule the task
        self.schedule_task(task)
        
        # Log action
        username = query.from_user.username or query.from_user.first_name or "Unknown"
        self.log_user_action(
            user_id, 
            username, 
            "TASK_CREATED",
            f"Task ID #{task_id}, Message: '{task_data['message']}', Date: {task_data['date']} {task_data['time']}, Frequency: {task_data['frequency']}"
        )
        
        # Show confirmation
        groups_data = self.load_json(GROUPS_FILE)
        groups = groups_data.get('groups', {})
        selected_names = []
        for gid in task_data['selected_groups']:
            if gid == 'myself':
                selected_names.append('Myself')
            else:
                selected_names.append(self.escape_markdown(groups.get(gid, gid)))
        
        confirmation = f"""
 **Task Created Successfully!**

📋 **Task Details:**
🆔 Task ID: #{task_id}
💬 Message: {task_data['message']}
📅 Date: {self.format_date_display(task_data['date'])}
⏰ Time: {task_data['time']}
👥 Groups: {', '.join(selected_names)}
🔁 Frequency: {task_data['frequency'].upper()}

The reminder will be sent automatically at the scheduled time.
        """
        
        await query.edit_message_text(confirmation, parse_mode='Markdown')
        
        # Clear session tracking and temp data
        await self.clear_conversation(user_id)
        del self.temp_task_data[user_id]
    
    def schedule_task(self, task: dict):
        """Schedule a task with APScheduler"""
        task_id = task['id']
        date_str = task['date']
        time_str = task['time']
        frequency = task['frequency']
        
        # Parse date and time
        year, month, day = map(int, date_str.split('-'))
        hour, minute = map(int, time_str.split(':'))
        
        job_id = f"task_{task_id}"
        
        if frequency == 'once':
            # One-time task
            # Use localize() for correct timezone offset with pytz
            naive_dt = datetime(year, month, day, hour, minute)
            run_date = TIMEZONE.localize(naive_dt)
            self.scheduler.add_job(
                self.send_reminder,
                trigger=DateTrigger(run_date=run_date),
                args=[task],
                id=job_id,
                replace_existing=True
            )
            logger.info(f"Scheduled one-time task #{task_id} for {run_date} (GMT+6)")
        elif frequency == 'daily':
            # Daily task
            self.scheduler.add_job(
                self.send_reminder,
                trigger=CronTrigger(hour=hour, minute=minute, timezone=TIMEZONE),
                args=[task],
                id=job_id,
                replace_existing=True
            )
            logger.info(f"Scheduled daily task #{task_id} for {hour:02d}:{minute:02d} (GMT+6)")
        elif frequency == 'weekly':
            # Weekly task - repeats on the same day of week
            # Get the day of week from the selected date (0=Monday, 6=Sunday)
            naive_dt = datetime(year, month, day)
            day_of_week = naive_dt.weekday()
            self.scheduler.add_job(
                self.send_reminder,
                trigger=CronTrigger(day_of_week=day_of_week, hour=hour, minute=minute, timezone=TIMEZONE),
                args=[task],
                id=job_id,
                replace_existing=True
            )
            days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
            logger.info(f"Scheduled weekly task #{task_id} for every {days[day_of_week]} at {hour:02d}:{minute:02d} (GMT+6)")
        elif frequency == 'monthly':
            # Monthly task - repeats on the same day of month
            self.scheduler.add_job(
                self.send_reminder,
                trigger=CronTrigger(day=day, hour=hour, minute=minute, timezone=TIMEZONE),
                args=[task],
                id=job_id,
                replace_existing=True
            )
            logger.info(f"Scheduled monthly task #{task_id} for day {day} at {hour:02d}:{minute:02d} (GMT+6)")
    
    async def send_reminder(self, task: dict):
        """Send reminder to groups"""
        message = task['message']
        groups = task['groups']
        task_id = task['id']
        created_by = task.get('created_by')  # Get task creator ID
        
        logger.info(f"Executing reminder for task #{task_id}: '{message[:30]}...'")
        
        app = Application.builder().token(self.token).build()
        
        # Create inline keyboard with acknowledgment button
        keyboard = [[InlineKeyboardButton("✅", callback_data=f"ack_{task_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        for group_id in groups:
            try:
                # Check if this is a personal reminder to the task creator
                if group_id == 'myself' and created_by:
                    # Send personal message to task creator
                    await app.bot.send_message(
                        chat_id=created_by,
                        text=message,
                        reply_markup=reply_markup
                    )
                    logger.info(f"Sent personal reminder to task creator {created_by}")
                else:
                    # Send to group as normal
                    await app.bot.send_message(
                        chat_id=int(group_id),
                        text=message,
                        reply_markup=reply_markup
                    )
                    logger.info(f"Sent reminder to group {group_id}")
            except Exception as e:
                logger.error(f"Failed to send reminder to {group_id}: {e}")
        
        # If it's a one-time task, mark it as completed
        if task['frequency'] == 'once':
            tasks = self.load_json(TASKS_FILE)
            for t in tasks['tasks']:
                if t['id'] == task['id']:
                    t['completed'] = True
                    break
            self.save_json(TASKS_FILE, tasks)
    
    async def task_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show all tasks with pagination"""
        if not self.is_authorized(update.effective_user.id):
            await update.message.reply_text("You are not authorized to use this.")
            return
        
        # Cancel any active session
        await self.cancel_active_session_if_exists(update, context)
        
        user_id = update.effective_user.id
        page = 0  # Start at page 0
        
        # Log action
        username = update.effective_user.username or update.effective_user.first_name or "Unknown"
        self.log_user_action(user_id, username, "VIEWED_TASK_LIST", "")
        
        await self.show_task_page(update.message, user_id, page, context)
    
    async def show_task_page(self, message, user_id: int, page: int, context: ContextTypes.DEFAULT_TYPE):
        """Show a specific page of tasks"""
        tasks = self.load_json(TASKS_FILE)
        task_list = tasks.get('tasks', [])
        
        # Filter out completed tasks
        all_active_tasks = [t for t in task_list if not t.get('completed')]
        
        is_admin = self.is_admin(user_id)
        
        # Filter tasks based on user role
        if is_admin:
            # Admins see all tasks
            active_tasks = all_active_tasks
        else:
            # Regular users see only their own tasks
            active_tasks = [t for t in all_active_tasks if t.get('created_by') == user_id]
        
        if not active_tasks:
            if is_admin:
                await message.reply_text("📭 No tasks scheduled.")
            else:
                await message.reply_text("📭 You have no tasks scheduled.")
            return
        
        groups_data = self.load_json(GROUPS_FILE)
        groups = groups_data.get('groups', {})
        
        # Pagination settings
        tasks_per_page = 10
        total_pages = (len(active_tasks) + tasks_per_page - 1) // tasks_per_page
        
        # Ensure page is within bounds
        page = max(0, min(page, total_pages - 1))
        
        # Get tasks for current page
        start_idx = page * tasks_per_page
        end_idx = min(start_idx + tasks_per_page, len(active_tasks))
        page_tasks = active_tasks[start_idx:end_idx]
        
        # Build message
        if is_admin:
            header = f"📋 **All Scheduled Tasks** ({len(active_tasks)} total)\n\n"
        else:
            header = f"📋 **Your Scheduled Tasks** ({len(active_tasks)} total)\n\n"
        message_text = header
        
        for task in page_tasks:
            group_names = []
            for gid in task['groups']:
                if gid == 'myself':
                    group_names.append('Myself')
                else:
                    group_names.append(self.escape_markdown(groups.get(gid, gid)))
            creator_id = task.get('created_by')
            creator_name = await self.get_user_name(creator_id, context) if creator_id else 'Unknown'
            
            # Always show creator name
            ownership_indicator = f"👤 {creator_name}"
            
            message_text += f"**#{task['id']}** - ⏳ Pending\n"
            message_text += f"💬 {self.escape_markdown(task['message'])}\n"
            message_text += f"📅 {self.format_date_display(task['date'])} at {task['time']}\n"
            message_text += f"🔁 {task['frequency'].upper()}\n"
            message_text += f"👥 {', '.join(group_names)}\n"
            # Only show ownership for admins (since regular users only see their own)
            if is_admin:
                message_text += f"{ownership_indicator}\n"
            message_text += self.escape_markdown('_' * 20) + "\n\n"
        
        # Add page info
        message_text += f"📄 Page {page + 1}/{total_pages}"
        
        # Create navigation buttons
        keyboard = []
        buttons = []
        
        if page > 0:
            buttons.append(InlineKeyboardButton("◀️ Previous", callback_data=f"page_{page-1}"))
        
        if page < total_pages - 1:
            buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f"page_{page+1}"))
        
        if buttons:
            keyboard.append(buttons)
        
        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        
        # Store pagination data
        self.pagination_data[user_id] = {'page': page, 'total_pages': total_pages}
        
        await message.reply_text(message_text, parse_mode='Markdown', reply_markup=reply_markup)
    
    async def handle_pagination(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle pagination button clicks"""
        query = update.callback_query
        await query.answer()
        
        user_id = update.effective_user.id
        data = query.data
        
        if data.startswith('page_'):
            page = int(data.split('_')[1])
            
            # Delete old message
            await query.message.delete()
            
            # Show new page
            tasks = self.load_json(TASKS_FILE)
            task_list = tasks.get('tasks', [])
            all_active_tasks = [t for t in task_list if not t.get('completed')]
            
            is_admin = self.is_admin(user_id)
            
            # Filter tasks based on user role
            if is_admin:
                active_tasks = all_active_tasks
            else:
                active_tasks = [t for t in all_active_tasks if t.get('created_by') == user_id]
            
            if not active_tasks:
                if is_admin:
                    await query.message.reply_text("📭 No tasks scheduled.")
                else:
                    await query.message.reply_text("📭 You have no tasks scheduled.")
                return
            
            groups_data = self.load_json(GROUPS_FILE)
            groups = groups_data.get('groups', {})
            
            # Pagination settings
            tasks_per_page = 10
            total_pages = (len(active_tasks) + tasks_per_page - 1) // tasks_per_page
            
            # Ensure page is within bounds
            page = max(0, min(page, total_pages - 1))
            
            # Get tasks for current page
            start_idx = page * tasks_per_page
            end_idx = min(start_idx + tasks_per_page, len(active_tasks))
            page_tasks = active_tasks[start_idx:end_idx]
            
            # Build message
            if is_admin:
                header = f"📋 **All Scheduled Tasks** ({len(active_tasks)} total)\n\n"
            else:
                header = f"📋 **Your Scheduled Tasks** ({len(active_tasks)} total)\n\n"
            message_text = header
            
            for task in page_tasks:
                group_names = []
                for gid in task['groups']:
                    if gid == 'myself':
                        group_names.append('Myself')
                    else:
                        group_names.append(self.escape_markdown(groups.get(gid, gid)))
                creator_id = task.get('created_by')
                creator_name = await self.get_user_name(creator_id, context) if creator_id else 'Unknown'
                
                # Always show creator name
                ownership_indicator = f"👤 {creator_name}"
                
                message_text += f"**#{task['id']}** - ⏳ Pending\n"
                message_text += f"💬 {self.escape_markdown(task['message'])}\n"
                message_text += f"📅 {self.format_date_display(task['date'])} at {task['time']}\n"
                message_text += f"🔁 {task['frequency'].upper()}\n"
                message_text += f"👥 {', '.join(group_names)}\n"
                # Only show ownership for admins
                if is_admin:
                    message_text += f"{ownership_indicator}\n"
                message_text += self.escape_markdown('_' * 20) + "\n\n"
            
            # Add page info
            message_text += f"📄 Page {page + 1}/{total_pages}"
            
            # Create navigation buttons
            keyboard = []
            buttons = []
            
            if page > 0:
                buttons.append(InlineKeyboardButton("◀️ Previous", callback_data=f"page_{page-1}"))
            
            if page < total_pages - 1:
                buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f"page_{page+1}"))
            
            if buttons:
                keyboard.append(buttons)
            
            reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
            
            # Store pagination data
            self.pagination_data[user_id] = {'page': page, 'total_pages': total_pages}
            
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=message_text,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
    
    async def handle_delete_pagination(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle delete task pagination button clicks"""
        query = update.callback_query
        await query.answer()
        
        user_id = update.effective_user.id
        data = query.data
        
        if data.startswith('delpage_'):
            page = int(data.split('_')[1])
            
            # Delete old message
            await query.message.delete()
            
            # Show new page using the stored task list
            task_list = context.user_data.get('delete_task_list', [])
            is_admin = context.user_data.get('is_admin', False)
            
            if not task_list:
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text="📭 No tasks to delete."
                )
                return
            
            # Load groups data to display group names
            groups_data = self.load_json(GROUPS_FILE)
            groups = groups_data.get('groups', {})
            
            # Pagination settings
            tasks_per_page = 10
            total_pages = (len(task_list) + tasks_per_page - 1) // tasks_per_page
            
            # Ensure page is within bounds
            page = max(0, min(page, total_pages - 1))
            
            # Get tasks for current page
            start_idx = page * tasks_per_page
            end_idx = min(start_idx + tasks_per_page, len(task_list))
            page_tasks = task_list[start_idx:end_idx]
            
            # Build header
            if is_admin:
                header = f"🗑️ **Delete Tasks - Interactive Mode**\n\n**All Tasks ({len(task_list)} total):**\n\n"
            else:
                header = f"🗑️ **Delete Tasks - Interactive Mode**\n\n**Your Tasks ({len(task_list)} total):**\n\n"
            message_text = header
            
            # Build task list for current page
            for idx, task in enumerate(page_tasks, start_idx + 1):
                # Handle "myself" in groups display and convert group IDs to names
                group_display = []
                for gid in task.get('groups', []):
                    if gid == 'myself':
                        group_display.append('Myself')
                    else:
                        # Look up group name, fallback to ID if not found
                        group_name = groups.get(gid, gid)
                        group_display.append(self.escape_markdown(group_name))
                groups_str = ', '.join(group_display)
                creator_id = task.get('created_by')
                creator_name = await self.get_user_name(creator_id, context) if creator_id else 'Unknown'
                
                # Escape task message for markdown
                task_message = self.escape_markdown(task['message'])
                
                # Always show creator name
                ownership_info = f"   **Created by:** {creator_name}\n"
                
                message_text += (
                    f"{idx}. {task_message}\n"
                    f"   **Date:** {self.format_date_display(task['date'])} {task['time']}\n"
                    f"   **Groups:** {groups_str}\n"
                    f"   **Frequency:** {task.get('frequency', 'Once')}\n"
                    f"{ownership_info}"
                    f"""{self.escape_markdown('_' * 20)}\n\n"""
                )
            
            # Add page info and footer
            message_text += f"📄 Page {page + 1}/{total_pages}\n\n"
            message_text += (
                "Send serial number(s) to delete (space-separated):\n"
                "Example: 1 3 5\n\n"
                "• Type /done when finished\n"
                "• Session auto-closes in 5 minutes"
            )
            
            # Create navigation buttons
            keyboard = []
            buttons = []
            
            if page > 0:
                buttons.append(InlineKeyboardButton("◀️ Previous", callback_data=f"delpage_{page-1}"))
            
            if page < total_pages - 1:
                buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f"delpage_{page+1}"))
            
            if buttons:
                keyboard.append(buttons)
            
            reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
            
            # Store current page
            context.user_data['delete_page'] = page
            
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=message_text,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
            return WAITING_FOR_DEL_TASKS
    
    async def del_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start interactive delete task session"""
        if not self.is_authorized(update.effective_user.id):
            await update.message.reply_text("You are not authorized to use this.")
            return ConversationHandler.END
        
        # Check and notify about previous session
        await self.check_and_cancel_previous_session(update, context, 'del_task')
        
        user_id = update.effective_user.id
        is_admin = self.is_admin(user_id)
        
        tasks = self.load_json(TASKS_FILE)
        all_tasks = tasks.get('tasks', [])
        
        # Filter tasks: admins see all, regular users see only their own
        if is_admin:
            task_list = all_tasks
        else:
            task_list = [task for task in all_tasks if task.get('created_by') == user_id]
        
        if not task_list:
            await self.clear_conversation(update.effective_user.id)
            if is_admin:
                await update.message.reply_text("📭 No tasks to delete.")
            else:
                await update.message.reply_text("📭 You have no tasks to delete.")
            return ConversationHandler.END
        
        # Store filtered task list in context for later use
        context.user_data['delete_task_list'] = task_list
        context.user_data['is_admin'] = is_admin
        context.user_data['delete_page'] = 0  # Start at page 0
        
        # Show first page
        await self.show_delete_task_page(update.message, user_id, 0, context)
        
        return WAITING_FOR_DEL_TASKS
    
    async def show_delete_task_page(self, message, user_id: int, page: int, context: ContextTypes.DEFAULT_TYPE):
        """Show a specific page of tasks for deletion"""
        task_list = context.user_data.get('delete_task_list', [])
        is_admin = context.user_data.get('is_admin', False)
        
        if not task_list:
            await message.reply_text("📭 No tasks to delete.")
            return
        
        # Load groups data to display group names
        groups_data = self.load_json(GROUPS_FILE)
        groups = groups_data.get('groups', {})
        
        # Pagination settings
        tasks_per_page = 10
        total_pages = (len(task_list) + tasks_per_page - 1) // tasks_per_page
        
        # Ensure page is within bounds
        page = max(0, min(page, total_pages - 1))
        
        # Get tasks for current page
        start_idx = page * tasks_per_page
        end_idx = min(start_idx + tasks_per_page, len(task_list))
        page_tasks = task_list[start_idx:end_idx]
        
        # Build header
        if is_admin:
            header = f"🗑️ **Delete Tasks - Interactive Mode**\n\n**All Tasks ({len(task_list)} total):**\n\n"
        else:
            header = f"🗑️ **Delete Tasks - Interactive Mode**\n\n**Your Tasks ({len(task_list)} total):**\n\n"
        message_text = header
        
        # Build task list for current page
        for idx, task in enumerate(page_tasks, start_idx + 1):
            # Handle "myself" in groups display and convert group IDs to names
            group_display = []
            for gid in task.get('groups', []):
                if gid == 'myself':
                    group_display.append('Myself')
                else:
                    # Look up group name, fallback to ID if not found
                    group_name = groups.get(gid, gid)
                    group_display.append(self.escape_markdown(group_name))
            groups_str = ', '.join(group_display)
            creator_id = task.get('created_by')
            creator_name = await self.get_user_name(creator_id, context) if creator_id else 'Unknown'
            
            # Escape task message for markdown
            task_message = self.escape_markdown(task['message'])
            
            # Always show creator name
            ownership_info = f"   **Created by:** {creator_name}\n"
            
            message_text += (
                f"{idx}. {task_message}\n"
                f"   **Date:** {self.format_date_display(task['date'])} {task['time']}\n"
                f"   **Groups:** {groups_str}\n"
                f"   **Frequency:** {task.get('frequency', 'Once')}\n"
                f"{ownership_info}"
                f"""{self.escape_markdown('_' * 20)}\n\n"""
            )
        
        # Add page info and footer
        message_text += f"📄 Page {page + 1}/{total_pages}\n\n"
        message_text += (
            "Send serial number(s) to delete (space-separated):\n"
            "Example: 1 3 5\n\n"
            "• Type /done when finished\n"
            "• Session auto-closes in 5 minutes"
        )
        
        # Create navigation buttons
        keyboard = []
        buttons = []
        
        if page > 0:
            buttons.append(InlineKeyboardButton("◀️ Previous", callback_data=f"delpage_{page-1}"))
        
        if page < total_pages - 1:
            buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f"delpage_{page+1}"))
        
        if buttons:
            keyboard.append(buttons)
        
        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        
        # Store current page
        context.user_data['delete_page'] = page
        
        await message.reply_text(message_text, parse_mode='Markdown', reply_markup=reply_markup)
    
    async def receive_del_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive and delete tasks"""
        user_id = update.effective_user.id
        
        # Check if this session is still active (not replaced by another command)
        if user_id in self.active_conversations and self.active_conversations[user_id] != 'del_task':
            # Forward to the correct handler and end this conversation
            await self.forward_to_active_session(update, context, user_id)
            return ConversationHandler.END
        
        text = update.message.text.strip()
        
        user_id = update.effective_user.id
        is_admin = context.user_data.get('is_admin', False)
        
        tasks = self.load_json(TASKS_FILE)
        all_tasks = tasks.get('tasks', [])
        
        # Get the filtered task list from context
        task_list = context.user_data.get('delete_task_list', [])
        
        # Parse serial numbers (space or newline separated)
        try:
            serial_numbers = [int(num.strip()) for num in text.replace('\n', ' ').split()]
        except ValueError:
            await update.message.reply_text("❌ Invalid input. Please provide serial numbers only (e.g., 1 2 3)")
            return WAITING_FOR_DEL_TASKS
        
        deleted = []
        not_found = []
        permission_denied = []
        
        # Sort in reverse to delete from end to beginning (avoid index shifting)
        for serial in sorted(serial_numbers, reverse=True):
            if 1 <= serial <= len(task_list):
                task = task_list[serial - 1]
                task_id = task['id']
                
                # Check ownership: only creator or admin can delete
                if not is_admin and task.get('created_by') != user_id:
                    permission_denied.append(f"#{task_id}")
                    continue
                
                # Find and remove from all_tasks list
                for i, t in enumerate(all_tasks):
                    if t['id'] == task_id:
                        # Remove from scheduler
                        job_id = f"task_{task_id}"
                        try:
                            self.scheduler.remove_job(job_id)
                        except:
                            pass
                        
                        all_tasks.pop(i)
                        deleted.append(f"#{task_id}: {task['message'][:30]}...")
                        break
                
                # Also remove from filtered list
                task_list.pop(serial - 1)
            else:
                not_found.append(str(serial))
        
        # Update context with new filtered list
        context.user_data['delete_task_list'] = task_list
        
        if deleted:
            tasks['tasks'] = all_tasks
            self.save_json(TASKS_FILE, tasks)
            
            # Cleanup and renumber remaining tasks
            self.cleanup_tasks()
            
            # Log action
            username = update.effective_user.username or update.effective_user.first_name or "Unknown"
            self.log_user_action(
                user_id,
                username,
                "TASK_DELETED",
                f"Deleted {len(deleted)} task(s): {', '.join(deleted)}"
            )
        
        response = ""
        if deleted:
            response += f"✅ **Deleted {len(deleted)} task(s):**\n" + "\n".join([f"• {d}" for d in deleted]) + "\n"
        if not_found:
            response += f"\n⚠️ **Serial number(s) not found:** {', '.join(not_found)}\n"
        if permission_denied:
            response += f"\n🚫 **Permission denied (not your tasks):** {', '.join(permission_denied)}\n"
        
        response += "\nSend more serial numbers or /done to finish."
        
        await update.message.reply_text(response, parse_mode='Markdown')
        return WAITING_FOR_DEL_TASKS
    
    async def add_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start interactive add user session (Admin only)"""
        if not self.is_admin(update.effective_user.id):
            await update.message.reply_text("❌ Only admins can add users.")
            return ConversationHandler.END
        
        # Check and notify about previous session
        await self.check_and_cancel_previous_session(update, context, 'add_user')
        
        await update.message.reply_text(
            "📝 **Add Users - Interactive Mode**\n\n"
            "Send user ID one per line like below:\n"
            "`123456789`\n"
            "`987654321`\n\n"
            "• Type /done when finished\n"
            "• Session auto-closes in 5 minutes\n\n"
            "💡 Forward user's message to this bot to get their ID",
            parse_mode='Markdown'
        )
        return WAITING_FOR_ADD_USERS
    
    async def receive_add_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive and add users"""
        user_id = update.effective_user.id
        
        # Check if this session is still active (not replaced by another command)
        if user_id in self.active_conversations and self.active_conversations[user_id] != 'add_user':
            # Forward to the correct handler and end this conversation
            await self.forward_to_active_session(update, context, user_id)
            return ConversationHandler.END
        
        text = update.message.text.strip()
        
        allowed_data = self.load_json(ALLOWED_USERS_FILE)
        if 'allowed_users' not in allowed_data:
            allowed_data['allowed_users'] = []
        
        # Get super admin ID to check restrictions
        super_admin_id = os.getenv('SUPER_ADMIN_ID')
        if super_admin_id:
            super_admin_id = int(super_admin_id)
        
        # Parse user IDs (space or newline separated)
        lines = text.split('\n')
        added = []
        already_exists = []
        invalid = []
        restricted = []
        
        for line in lines:
            user_id_str = line.strip()
            
            # Skip empty lines
            if not user_id_str:
                continue
            
            try:
                user_id = int(user_id_str)
                
                # Check if trying to add super admin
                if super_admin_id and user_id == super_admin_id:
                    restricted.append(user_id)
                    continue
                
                # Check if user already exists
                user_exists = False
                for existing_user in allowed_data['allowed_users']:
                    if isinstance(existing_user, dict) and existing_user.get('user_id') == user_id:
                        user_exists = True
                        break
                    elif isinstance(existing_user, int) and existing_user == user_id:
                        user_exists = True
                        break
                
                if user_exists:
                    already_exists.append(user_id)
                else:
                    added.append(user_id)
            except ValueError:
                invalid.append(user_id_str)
        
        # Fetch names for added users and store them
        added_with_names = []
        for uid in added:
            try:
                chat = await context.bot.get_chat(uid)
                name = chat.first_name or "Unknown"
                if chat.last_name:
                    name += f" {chat.last_name}"
                
                # Add user as dictionary with name
                allowed_data['allowed_users'].append({
                    'user_id': uid,
                    'name': name
                })
                added_with_names.append((uid, name))
            except:
                # If can't fetch name, add with Unknown
                allowed_data['allowed_users'].append({
                    'user_id': uid,
                    'name': 'Unknown'
                })
                added_with_names.append((uid, "Unknown"))
        
        if added or already_exists:
            self.save_json(ALLOWED_USERS_FILE, allowed_data)
        
        # Fetch names for already existing users
        exists_with_names = []
        for uid in already_exists:
            try:
                chat = await context.bot.get_chat(uid)
                name = chat.first_name or "Unknown"
                if chat.last_name:
                    name += f" {chat.last_name}"
                exists_with_names.append((uid, name))
            except:
                exists_with_names.append((uid, "Unknown"))
        
        # Log action
        if added_with_names:
            username = update.effective_user.username or update.effective_user.first_name or "Unknown"
            user_list = ', '.join([f"{name} ({uid})" for uid, name in added_with_names])
            self.log_user_action(
                update.effective_user.id,
                username,
                "USER_ADDED",
                f"Added {len(added_with_names)} user(s): {user_list}"
            )
        
        response = ""
        if added_with_names:
            response += f"✅ **Added {len(added_with_names)} user(s):**\n" + "\n".join([f"• **{name}** (`{uid}`)" for uid, name in added_with_names]) + "\n\n"
        if exists_with_names:
            response += f"⚠️ **Already authorized:**\n" + "\n".join([f"• **{name}** (`{uid}`)" for uid, name in exists_with_names]) + "\n\n"
        if restricted:
            response += f"🚫 **Restricted:**\n" + "\n".join([f"• This person is unavailable to add as admin. Settings Restricted (`{uid}`)" for uid in restricted]) + "\n\n"
        if invalid:
            response += f"❌ **Invalid ID(s):**\n" + "\n".join([f"• {inv}" for inv in invalid]) + "\n\n"
        
        if not added and not already_exists and not invalid and not restricted:
            response = "❌ Invalid format. Send user ID: `123456789`\n\n"
        
        response += "Send more user IDs or /done to finish."
        
        await update.message.reply_text(response, parse_mode='Markdown')
        return WAITING_FOR_ADD_USERS
    
    async def remove_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start interactive remove user session (Admin only)"""
        user_id = update.effective_user.id
        if not self.is_admin(user_id):
            await update.message.reply_text("❌ Only admins can remove users.")
            return ConversationHandler.END
        
        # Check and notify about previous session
        await self.check_and_cancel_previous_session(update, context, 'remove_user')
        
        allowed_data = self.load_json(ALLOWED_USERS_FILE)
        allowed_list = allowed_data.get('allowed_users', [])
        
        # Get super admin ID
        super_admin_id = os.getenv('SUPER_ADMIN_ID')
        if super_admin_id:
            super_admin_id = int(super_admin_id)
        
        # Get complete admin list (including super admin)
        admin_list = self.get_admin_list()
        
        # Separate super admin and regular admins
        super_admin_user = None
        admin_users = []
        
        for uid in admin_list:
            # Check if this is the super admin
            if super_admin_id and uid == super_admin_id:
                # Only show super admin to super admin themselves
                if user_id == super_admin_id:
                    try:
                        chat = await context.bot.get_chat(uid)
                        name = chat.first_name or "Unknown"
                        if chat.last_name:
                            name += f" {chat.last_name}"
                        super_admin_user = (uid, name)
                    except:
                        super_admin_user = (uid, "Unknown")
                continue  # Skip adding to regular admin list
            
            # Regular admins
            try:
                chat = await context.bot.get_chat(uid)
                name = chat.first_name or "Unknown"
                if chat.last_name:
                    name += f" {chat.last_name}"
                admin_users.append((uid, name))
            except:
                admin_users.append((uid, "Unknown"))
        
        # Fetch regular user names
        regular_users = []
        for user in allowed_list:
            # Handle both dict and int formats
            if isinstance(user, dict):
                uid = user.get('user_id')
                name = user.get('name', 'Unknown')
            else:
                uid = user
                name = 'Unknown'
            
            if uid not in admin_list:
                regular_users.append((uid, name))
        
        if not super_admin_user and not admin_users and not regular_users:
            await self.clear_conversation(update.effective_user.id)
            await update.message.reply_text("📭 No users to remove.")
            return ConversationHandler.END
        
        # Build user list display with serial numbers
        user_display = ""
        serial = 1
        
        # Super Admin section (only visible to super admin)
        if super_admin_user:
            user_display += "**Super Admin:**\n"
            uid, name = super_admin_user
            user_display += f"{serial}. {name} (`{uid}`) 🌟\n"
            serial += 1
            user_display += "\n"
        
        # Admins section
        if admin_users:
            user_display += "**Admin:**\n"
            for uid, name in admin_users:
                user_display += f"{serial}. {name} (`{uid}`) 👑\n"
                serial += 1
            user_display += "\n"
        
        if regular_users:
            user_display += "**Authorized Users:**\n"
            for uid, name in regular_users:
                user_display += f"{serial}. {name} (`{uid}`)\n"
                serial += 1
        
        await update.message.reply_text(
            f"🗑️ **Remove Users - Interactive Mode**\n\n"
            f"{user_display}\n\n"
            "Send serial number(s) to remove (space-separated):\n"
            "Example:  3 4 5 ..\n\n"
            "⚠️ Note: Cannot remove admins\n\n"
            "• Type /done when finished\n"
            "• Session auto-closes in 5 minutes",
            parse_mode='Markdown'
        )
        return WAITING_FOR_DEL_USERS
    
    async def receive_del_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive and delete users"""
        user_id = update.effective_user.id
        
        # Check if this session is still active (not replaced by another command)
        if user_id in self.active_conversations and self.active_conversations[user_id] != 'remove_user':
            # Forward to the correct handler and end this conversation
            await self.forward_to_active_session(update, context, user_id)
            return ConversationHandler.END
        
        text = update.message.text.strip()
        
        allowed_data = self.load_json(ALLOWED_USERS_FILE)
        if 'allowed_users' not in allowed_data:
            allowed_data['allowed_users'] = []
        
        # Get super admin ID
        super_admin_id = os.getenv('SUPER_ADMIN_ID')
        if super_admin_id:
            super_admin_id = int(super_admin_id)
        
        # Get complete admin list to exclude from deletion (including super admin)
        admin_list = self.get_admin_list()
        
        # Build combined user list with types
        all_users = []
        
        # Add super admin first (only if viewer is super admin)
        if super_admin_id and user_id == super_admin_id:
            for uid in admin_list:
                if uid == super_admin_id:
                    name = await self.get_user_name(uid, context)
                    all_users.append((uid, name, 'SuperAdmin'))
                    break
        
        # Add regular admins (excluding super admin)
        for uid in admin_list:
            if super_admin_id and uid == super_admin_id:
                continue  # Skip super admin, already added above
            
            name = await self.get_user_name(uid, context)
            all_users.append((uid, name, 'Admin'))
        
        for user in allowed_data['allowed_users']:
            # Handle both dict and int formats
            if isinstance(user, dict):
                uid = user.get('user_id')
                name = user.get('name', 'Unknown')
            else:
                uid = user
                name = 'Unknown'
            
            if uid not in admin_list:
                all_users.append((uid, name, 'User'))
        
        # Parse serial numbers (space or newline separated)
        try:
            serial_numbers = [int(num.strip()) for num in text.replace('\n', ' ').split()]
        except ValueError:
            await update.message.reply_text("❌ Invalid input. Please provide serial numbers only (e.g., 1 2 3)")
            return WAITING_FOR_DEL_USERS
        
        removed = []
        not_found = []
        is_admin = []
        
        for serial in serial_numbers:
            if 1 <= serial <= len(all_users):
                user_id, user_name, user_type = all_users[serial - 1]
                if user_type in ['Admin', 'SuperAdmin']:
                    is_admin.append((user_id, user_name))
                else:
                    # Find and remove user from list
                    removed_user = False
                    for i, user in enumerate(allowed_data['allowed_users']):
                        # Handle both dict and int formats
                        if isinstance(user, dict) and user.get('user_id') == user_id:
                            allowed_data['allowed_users'].pop(i)
                            removed.append((user_id, user_name))
                            removed_user = True
                            break
                        elif isinstance(user, int) and user == user_id:
                            allowed_data['allowed_users'].pop(i)
                            removed.append((user_id, user_name))
                            removed_user = True
                            break
                    
                    if not removed_user:
                        not_found.append(str(serial))
            else:
                not_found.append(str(serial))
        
        if removed:
            self.save_json(ALLOWED_USERS_FILE, allowed_data)
            
            # Log action
            username = update.effective_user.username or update.effective_user.first_name or "Unknown"
            user_list = ', '.join([f"{name} ({uid})" for uid, name in removed])
            self.log_user_action(
                update.effective_user.id,
                username,
                "USER_REMOVED",
                f"Removed {len(removed)} user(s): {user_list}"
            )
        
        response = ""
        if removed:
            response += f"✅ **Removed {len(removed)} user(s):**\n" + "\n".join([f"• **{name}** (`{uid}`)" for uid, name in removed]) + "\n"
        if is_admin:
            response += f"\n⚠️ **Cannot remove (Admins):**\n" + "\n".join([f"• **{name}** (`{uid}`) 👑" for uid, name in is_admin]) + "\n"
        if not_found:
            response += f"\n⚠️ **Serial number(s) not found:** {', '.join(not_found)}\n"
        
        response += "\nSend more serial numbers or /done to finish."
        
        await update.message.reply_text(response, parse_mode='Markdown')
        return WAITING_FOR_DEL_USERS
    
    async def list_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List all authorized users (Admin only)"""
        user_id = update.effective_user.id
        if not self.is_admin(user_id):
            await update.message.reply_text("❌ Only admins can view user list.")
            return
        
        # Cancel any active session
        await self.cancel_active_session_if_exists(update, context)
        
        # Log action
        username = update.effective_user.username or update.effective_user.first_name or "Unknown"
        self.log_user_action(user_id, username, "VIEWED_USER_LIST", "")
        
        # Get super admin ID
        super_admin_id = os.getenv('SUPER_ADMIN_ID')
        if super_admin_id:
            super_admin_id = int(super_admin_id)
            
        # Get complete admin list (including super admin)
        admin_list = self.get_admin_list()
        
        # Get allowed users from JSON
        allowed_data = self.load_json(ALLOWED_USERS_FILE)
        allowed_list = allowed_data.get('allowed_users', [])
        
        message = ""
        
        # Separate super admin and regular admins
        super_admin_user = None
        admin_users = []
        
        for uid in admin_list:
            # Check if this is the super admin
            if super_admin_id and uid == super_admin_id:
                # Only show super admin to super admin themselves
                if user_id == super_admin_id:
                    try:
                        chat = await context.bot.get_chat(uid)
                        name = chat.first_name or "Unknown"
                        if chat.last_name:
                            name += f" {chat.last_name}"
                        super_admin_user = (uid, name)
                    except:
                        super_admin_user = (uid, "Unknown")
                continue  # Skip adding to regular admin list
            
            # Regular admins
            try:
                chat = await context.bot.get_chat(uid)
                name = chat.first_name or "Unknown"
                if chat.last_name:
                    name += f" {chat.last_name}"
                admin_users.append((uid, name))
            except:
                admin_users.append((uid, "Unknown"))
        
        # Fetch regular user names
        regular_users = []
        for user in allowed_list:
            # Handle both dict and int formats
            if isinstance(user, dict):
                uid = user.get('user_id')
                name = user.get('name', 'Unknown')
            else:
                uid = user
                name = 'Unknown'
            
            if uid not in admin_list:
                regular_users.append((uid, name))
        
        # Build message - Super Admin section (only visible to super admin)
        if super_admin_user:
            message += "**Super Admin:**\n"
            uid, name = super_admin_user
            message += f"{name} (`{uid}`) 🌟\n\n"
        
        # Admins section
        if admin_users:
            message += "**Admins:**\n"
            for uid, name in admin_users:
                message += f"{name} (`{uid}`) 👑\n"
            message += "\n"
        
        if regular_users:
            message += "**Authorized Users:**\n"
            for uid, name in regular_users:
                message += f"{name} (`{uid}`)\n"
            message += "\n"
        
        if not admin_users and not regular_users:
            message += "No authorized users yet.\n\n"
        
        # Count all users including super admin (if visible)
        total_count = len(admin_users) + len(regular_users)
        if super_admin_user:
            total_count += 1
        message += f"**Total:** {total_count} user(s)"
        
        await update.message.reply_text(message, parse_mode='Markdown')
    
    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel conversation"""
        await self.clear_conversation(update.effective_user.id)
        await update.message.reply_text("❌ Operation cancelled.")
        return ConversationHandler.END
    
    async def timeout_add_groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle add groups session timeout"""
        if update.effective_user:
            await self.clear_conversation(update.effective_user.id)
            try:
                await context.bot.send_message(
                    chat_id=update.effective_user.id,
                    text="⏱️ <b>Add Groups</b> session expired after 5 minutes of inactivity.\n\nStart a new command anytime!",
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Failed to send timeout message: {e}")
        return ConversationHandler.END
    
    async def timeout_del_groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle delete groups session timeout"""
        if update.effective_user:
            await self.clear_conversation(update.effective_user.id)
            try:
                await context.bot.send_message(
                    chat_id=update.effective_user.id,
                    text="⏱️ <b>Delete Groups</b> session expired after 5 minutes of inactivity.\n\nStart a new command anytime!",
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Failed to send timeout message: {e}")
        return ConversationHandler.END
    
    async def timeout_add_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle add task session timeout"""
        if update.effective_user:
            await self.clear_conversation(update.effective_user.id)
            if update.effective_user.id in self.temp_task_data:
                del self.temp_task_data[update.effective_user.id]
            try:
                await context.bot.send_message(
                    chat_id=update.effective_user.id,
                    text="⏱️ <b>Add Task</b> session expired after 5 minutes of inactivity.\n\nStart a new command anytime!",
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Failed to send timeout message: {e}")
        return ConversationHandler.END
    
    async def timeout_del_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle delete tasks session timeout"""
        if update.effective_user:
            await self.clear_conversation(update.effective_user.id)
            try:
                await context.bot.send_message(
                    chat_id=update.effective_user.id,
                    text="⏱️ <b>Delete Tasks</b> session expired after 5 minutes of inactivity.\n\nStart a new command anytime!",
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Failed to send timeout message: {e}")
        return ConversationHandler.END
    
    async def timeout_add_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle add users session timeout"""
        if update.effective_user:
            await self.clear_conversation(update.effective_user.id)
            try:
                await context.bot.send_message(
                    chat_id=update.effective_user.id,
                    text="⏱️ <b>Add Users</b> session expired after 5 minutes of inactivity.\n\nStart a new command anytime!",
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Failed to send timeout message: {e}")
        return ConversationHandler.END
    
    async def timeout_del_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle remove users session timeout"""
        if update.effective_user:
            await self.clear_conversation(update.effective_user.id)
            try:
                await context.bot.send_message(
                    chat_id=update.effective_user.id,
                    text="⏱️ <b>Remove Users</b> session expired after 5 minutes of inactivity.\n\nStart a new command anytime!",
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Failed to send timeout message: {e}")
        return ConversationHandler.END
    
    async def finish_add_groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Finish add groups session"""
        await self.clear_conversation(update.effective_user.id)
        groups_data = self.load_json(GROUPS_FILE)
        group_list = "\n".join([f"• {self.escape_markdown(name)}: {self.escape_markdown(gid)}" for gid, name in groups_data.get('groups', {}).items()])
        await update.message.reply_text(
            f"✅ **Add Groups Session Completed**\n\n{group_list}",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    async def finish_del_groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Finish delete groups session"""
        await self.clear_conversation(update.effective_user.id)
        groups_data = self.load_json(GROUPS_FILE)
        group_list = "\n".join([f"• {self.escape_markdown(name)}: {self.escape_markdown(gid)}" for gid, name in groups_data.get('groups', {}).items()])
        await update.message.reply_text(
            f"✅ **Delete Groups Session Completed**\n\n{group_list}",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    async def finish_del_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Finish delete tasks session"""
        await self.clear_conversation(update.effective_user.id)
        tasks = self.load_json(TASKS_FILE)
        task_count = len(tasks.get('tasks', []))
        await update.message.reply_text(
            f"✅ **Delete Tasks Session Completed**\n\n📋 Total tasks: {task_count}",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    async def finish_del_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Finish delete users session"""
        user_id = update.effective_user.id
        await self.clear_conversation(user_id)
        allowed_data = self.load_json(ALLOWED_USERS_FILE)
        
        # Get super admin ID
        super_admin_id = os.getenv('SUPER_ADMIN_ID')
        if super_admin_id:
            super_admin_id = int(super_admin_id)
        
        # Count all users including admins (respecting visibility)
        admin_list = self.get_admin_list()
        
        # Only count super admin if current user IS the super admin
        visible_admin_count = 0
        for admin_id in admin_list:
            if super_admin_id and admin_id == super_admin_id:
                # Only count super admin if viewer is super admin
                if user_id == super_admin_id:
                    visible_admin_count += 1
            else:
                visible_admin_count += 1
        
        # Filter out admins from regular users
        actual_regular_users = []
        for user in allowed_data.get('allowed_users', []):
            uid = user.get('user_id') if isinstance(user, dict) else user
            if uid not in admin_list:
                actual_regular_users.append(uid)
        
        total_count = visible_admin_count + len(actual_regular_users)
        
        await update.message.reply_text(
            f"✅ **Remove Users Session Completed**\n\n👥 Total users: {total_count}",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    async def finish_add_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Finish add users session"""
        user_id = update.effective_user.id
        await self.clear_conversation(user_id)
        allowed_data = self.load_json(ALLOWED_USERS_FILE)
        
        # Get super admin ID
        super_admin_id = os.getenv('SUPER_ADMIN_ID')
        if super_admin_id:
            super_admin_id = int(super_admin_id)
        
        # Count all users including admins (respecting visibility)
        admin_list = self.get_admin_list()
        
        # Only count super admin if current user IS the super admin
        visible_admin_count = 0
        for admin_id in admin_list:
            if super_admin_id and admin_id == super_admin_id:
                # Only count super admin if viewer is super admin
                if user_id == super_admin_id:
                    visible_admin_count += 1
            else:
                visible_admin_count += 1
        
        # Filter out admins from regular users
        actual_regular_users = []
        for user in allowed_data.get('allowed_users', []):
            uid = user.get('user_id') if isinstance(user, dict) else user
            if uid not in admin_list:
                actual_regular_users.append(uid)
        
        total_count = visible_admin_count + len(actual_regular_users)
        
        await update.message.reply_text(
            f"✅ **Add Users Session Completed**\n\n👥 Total users: {total_count}",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    async def cancel_task_creation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel task creation"""
        user_id = update.effective_user.id
        await self.clear_conversation(user_id)
        # Clear temp task data
        if user_id in self.temp_task_data:
            del self.temp_task_data[user_id]
        await update.message.reply_text("❌ Task creation cancelled.")
        return ConversationHandler.END
    
    async def get_chat_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reply with chat/group ID in monospace"""
        # Only cancel active session if used in private chat, not in groups
        # Groups use this to get group ID, shouldn't interrupt user's active session
        chat_type = update.effective_chat.type
        if chat_type == 'private':
            await self.cancel_active_session_if_exists(update, context)
        
        chat_id = update.effective_chat.id
        await update.message.reply_text(f"`{chat_id}`", parse_mode='Markdown')
    
    async def handle_forwarded_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Extract user ID from forwarded message (any length)"""
        # Only respond to admins for security
        if not self.is_admin(update.effective_user.id):
            return
        
        user_id = None
        full_name = None
        username = None
        
        # Check for forwarded message info (works regardless of message length)
        if update.message.forward_from:
            # Direct forward from user (privacy not enabled)
            user_id = update.message.forward_from.id
            first_name = update.message.forward_from.first_name
            last_name = update.message.forward_from.last_name or ""
            full_name = f"{first_name} {last_name}".strip()
            username = update.message.forward_from.username
        elif update.message.forward_sender_name:
            # User has privacy enabled, only name available
            full_name = update.message.forward_sender_name
        elif update.message.forward_from_chat:
            # Forwarded from channel/group
            await update.message.reply_text(
                "⚠️ This is forwarded from a channel/group, not a personal account."
            )
            return
        
        # Display results (handles any message length)
        if user_id:
            response = f"👤 **User Info:**\n\n"
            if username:
                # Escape markdown characters in username
                safe_username = username.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace('`', '\\`')
                response += f"**Username:** @{safe_username}\n"
            response += f"**Name:** {full_name}\n"
            response += f"**ID:** `{user_id}`"
            await update.message.reply_text(response, parse_mode='Markdown')
        elif full_name:
            await update.message.reply_text(
                f"👤 **User Info:**\n\n"
                f"**Name:** {full_name}\n"
                f"⚠️ **ID:** Hidden (privacy settings enabled)",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                "⚠️ Cannot get user info. This message is not forwarded from a personal account."
            )
    
    async def post_init(self, application: Application):
        """Initialize scheduler and tasks after bot starts"""
        # Start scheduler
        self.scheduler.start()
        
        # Load and schedule existing tasks
        tasks = self.load_json(TASKS_FILE)
        for task in tasks.get('tasks', []):
            if not task.get('completed'):
                try:
                    self.schedule_task(task)
                except Exception as e:
                    logger.error(f"Failed to schedule task {task['id']}: {e}")

    def run(self):
        """Run the bot"""
        # Cleanup completed tasks on startup
        self.cleanup_tasks()
        
        # Create application
        app = Application.builder().token(self.token).post_init(self.post_init).build()
        
        # Add handlers
        app.add_handler(CommandHandler('start', self.start))
        app.add_handler(CommandHandler('help', self.help_command))
        app.add_handler(CommandHandler('task_list', self.task_list))
        app.add_handler(CommandHandler('id', self.get_chat_id))
        
        # Management menu handlers
        app.add_handler(CommandHandler('manage_groups', self.manage_groups))
        app.add_handler(CommandHandler('manage_users', self.manage_users))
        
        # Individual commands (accessible via menu or direct command)
        app.add_handler(CommandHandler('list_groups', self.list_groups))
        
        # User management handlers (Admin only)
        app.add_handler(CommandHandler('list_users', self.list_users))
        
        # Add group conversation handler (with 5-minute timeout)
        add_group_conv = ConversationHandler(
            entry_points=[
                CommandHandler('add_group', self.add_group),
                CallbackQueryHandler(self.handle_callback, pattern='^menu_add_group$')
            ],
            states={
                ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, self.timeout_add_groups)],
                WAITING_FOR_ADD_GROUPS: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.receive_add_groups)
                ]
            },
            fallbacks=[
                CommandHandler('done', self.finish_add_groups),
                CommandHandler('cancel', self.cancel)
            ],
            conversation_timeout=300,  # 5 minutes
            allow_reentry=True,
            name='add_group_conversation'
        )
        app.add_handler(add_group_conv)
        
        # Delete group conversation handler (with 5-minute timeout)
        del_group_conv = ConversationHandler(
            entry_points=[
                CommandHandler('del_group', self.del_group),
                CallbackQueryHandler(self.handle_callback, pattern='^menu_del_group$')
            ],
            states={
                ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, self.timeout_del_groups)],
                WAITING_FOR_DEL_GROUPS: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.receive_del_groups)
                ]
            },
            fallbacks=[
                CommandHandler('done', self.finish_del_groups),
                CommandHandler('cancel', self.cancel)
            ],
            conversation_timeout=300,  # 5 minutes
            allow_reentry=True,
            name='del_group_conversation'
        )
        app.add_handler(del_group_conv)
        
        # List groups handler
        app.add_handler(CommandHandler('list_groups', self.list_groups))
        
        # Task conversation handler (with 5-minute timeout)
        task_conv = ConversationHandler(
            entry_points=[CommandHandler('add_task', self.add_task_start)],
            states={
                ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, self.timeout_add_task)],
                WAITING_FOR_TASK_MESSAGE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.receive_task_message),
                    CallbackQueryHandler(self.handle_callback)
                ]
            },
            fallbacks=[
                CommandHandler('done', self.cancel_task_creation),
                CommandHandler('cancel', self.cancel)
            ],
            conversation_timeout=300,  # 5 minutes
            allow_reentry=True
        )
        app.add_handler(task_conv)
        
        # Delete task conversation handler (with 5-minute timeout)
        del_task_conv = ConversationHandler(
            entry_points=[CommandHandler('del_task', self.del_task)],
            states={
                ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, self.timeout_del_tasks)],
                WAITING_FOR_DEL_TASKS: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.receive_del_tasks)
                ]
            },
            fallbacks=[
                CommandHandler('done', self.finish_del_tasks),
                CommandHandler('cancel', self.cancel)
            ],
            conversation_timeout=300,  # 5 minutes
            allow_reentry=True,
            name='del_task_conversation'
        )
        app.add_handler(del_task_conv)
        
        # Remove user conversation handler (with 5-minute timeout)
        remove_user_conv = ConversationHandler(
            entry_points=[
                CommandHandler('remove_user', self.remove_user),
                CallbackQueryHandler(self.handle_callback, pattern='^menu_remove_user$')
            ],
            states={
                ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, self.timeout_del_users)],
                WAITING_FOR_DEL_USERS: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.receive_del_users)
                ]
            },
            fallbacks=[
                CommandHandler('done', self.finish_del_users),
                CommandHandler('cancel', self.cancel)
            ],
            conversation_timeout=300,  # 5 minutes
            allow_reentry=True,
            name='remove_user_conversation'
        )
        app.add_handler(remove_user_conv)
        
        # Add user conversation handler (with 5-minute timeout)
        add_user_conv = ConversationHandler(
            entry_points=[
                CommandHandler('add_user', self.add_user),
                CallbackQueryHandler(self.handle_callback, pattern='^menu_add_user$')
            ],
            states={
                ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, self.timeout_add_users)],
                WAITING_FOR_ADD_USERS: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.receive_add_users)
                ]
            },
            fallbacks=[
                CommandHandler('done', self.finish_add_users),
                CommandHandler('cancel', self.cancel)
            ],
            conversation_timeout=300,  # 5 minutes
            allow_reentry=True,
            name='add_user_conversation'
        )
        app.add_handler(add_user_conv)
        
        # Forwarded message handler (to get user IDs)
        app.add_handler(MessageHandler(filters.FORWARDED & ~filters.COMMAND, self.handle_forwarded_message))
        
        # Global callback handler (for pagination, acknowledgment, and non-conversation menu items)
        # Only handles menu_list_*, page_*, delpage_*, and ack_* patterns
        # Conversation starters (menu_add_*, menu_del_*, menu_remove_*) are handled by conversation handlers
        app.add_handler(CallbackQueryHandler(self.handle_callback, pattern='^(menu_list_|page_|delpage_|ack_)'))
        
        # Set bot commands menu and start scheduler
        async def post_init(application: Application):
            # Set commands
            await application.bot.set_my_commands([
                BotCommand("add_task", "📝 Create task(s)"),
                BotCommand("del_task", "🗑 Delete task(s)"),
                BotCommand("task_list", "📋 View all tasks"),
                BotCommand("manage_groups", "🗂 Manage groups"),
                BotCommand("manage_users", "👤 Manage users"),
                BotCommand("help", "❗️ Show command guide")
            ])
            
            # Start scheduler
            if not self.scheduler.running:
                self.scheduler.start()
                logger.info("Scheduler started!")
                
            # Load and schedule existing tasks
            tasks = self.load_json(TASKS_FILE)
            count = 0
            for task in tasks.get('tasks', []):
                if not task.get('completed'):
                    try:
                        self.schedule_task(task)
                        count += 1
                    except Exception as e:
                        logger.error(f"Failed to schedule task {task.get('id')}: {e}")
            logger.info(f"Loaded {count} active tasks")
        
        app.post_init = post_init
        
        # Start bot
        logger.info("Bot started!")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


def main():
    # Get bot token
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not found in environment variables!")
        return
    
    # Check admin users (supports comma-separated list, includes super admin)
    bot = ReminderBot(token)
    admin_list = bot.get_admin_list()  # Gets all admins including super admin
    
    if admin_list:
        # Load existing allowed users and merge
        existing_data = bot.load_json(ALLOWED_USERS_FILE)
        existing_users = existing_data.get('allowed_users', [])
        
        # Extract existing user IDs (handle both dict and int formats)
        existing_user_ids = []
        for user in existing_users:
            if isinstance(user, dict):
                existing_user_ids.append(user.get('user_id'))
            else:
                existing_user_ids.append(user)
        
        # Add new admin users that don't exist yet
        for admin_id in admin_list:
            if admin_id not in existing_user_ids:
                existing_users.append(admin_id)
        
        allowed_users = {'allowed_users': existing_users}
        bot.save_json(ALLOWED_USERS_FILE, allowed_users)
        
        logger.info(f"Authorized users: {existing_user_ids + [uid for uid in admin_list if uid not in existing_user_ids]}")
    
    # Start bot
    bot = ReminderBot(token)
    bot.run()


if __name__ == '__main__':
    main()
