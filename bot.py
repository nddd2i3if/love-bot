import asyncio
import logging
import sqlite3
import os
from datetime import date, timedelta
from typing import List, Dict

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile, ReplyKeyboardMarkup, KeyboardButton, BotCommand
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from openai import AsyncOpenAI

# ========== ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не задан в переменных окружения")
if not OPENROUTER_API_KEY:
    print("⚠️ OPENROUTER_API_KEY не задан, AI не будет работать")

# ========== НАСТРОЙКИ ==========
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
AVAILABLE_MODELS = {
    "openrouter/auto": "🤖 Auto (OpenRouter подберет сам)",
    "openai/gpt-4.1-nano": "🧠 GPT-4.1 Nano",
    "google/gemini-2.0-flash-exp:free": "✨ Gemini 2.0 Flash",
    "microsoft/phi-4-mini:free": "📘 Phi-4 Mini",
    "qwen/qwen-2.5-72b-instruct:free": "🐉 Qwen 2.5",
    "meta-llama/llama-4-scout:free": "🦙 Llama 4 Scout",
    "deepseek/deepseek-r1:free": "🧠 DeepSeek R1",
    "openrouter/free": "🆓 openrouter/free",
}
DEFAULT_MODEL = "openrouter/auto"

TIMEZONE = "Europe/Moscow"
RESET_HOUR = 0
RESET_MINUTE = 0
MAX_MESSAGE_LEN = 4000

# ========== ИНИЦИАЛИЗАЦИЯ ==========
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
client = AsyncOpenAI(base_url=OPENROUTER_BASE_URL, api_key=OPENROUTER_API_KEY)

# ========== ПОСТОЯННАЯ КЛАВИАТУРА ==========
def get_main_keyboard() -> ReplyKeyboardMarkup:
    button = KeyboardButton(text="🔮 Меню")
    return ReplyKeyboardMarkup(keyboard=[[button]], resize_keyboard=True)

# ========== БАЗА ДАННЫХ ==========
DB_NAME = "daily_tasks.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            total_completed INTEGER DEFAULT 0,
            last_activity_date TEXT,
            current_streak INTEGER DEFAULT 0
        )
    """)
    cur.execute("CREATE TABLE IF NOT EXISTS tasks_template (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_daily_tasks (
            user_id INTEGER, task_id INTEGER, task_text TEXT,
            completed BOOLEAN DEFAULT 0, task_date TEXT,
            PRIMARY KEY (user_id, task_id, task_date)
        )
    """)
    cur.execute("SELECT COUNT(*) FROM tasks_template")
    if cur.fetchone()[0] == 0:
        sample_tasks = [
            "СКИНУТЬ ЖЫВОТИК",
            "НОПИСАТЬ МНЕ",
            "ПАКУШАТЬ!"
        ]
        for task in sample_tasks:
            cur.execute("INSERT INTO tasks_template (text) VALUES (?)", (task,))
    conn.commit()
    conn.close()

def get_or_create_user(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, total_completed, last_activity_date, current_streak) VALUES (?, 0, NULL, 0)", (user_id,))
    conn.commit()
    conn.close()

def update_streak(user_id: int):
    today_str = date.today().isoformat()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM user_daily_tasks WHERE user_id=? AND task_date=? AND completed=1", (user_id, today_str))
    count_today = cur.fetchone()[0]
    if count_today == 0:
        conn.close()
        return
    cur.execute("SELECT last_activity_date, current_streak FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    last_date_str = row[0]
    current_streak = row[1] or 0
    last_date = date.fromisoformat(last_date_str) if last_date_str else None
    today = date.today()
    if last_date == today:
        conn.close()
        return
    elif last_date == today - timedelta(days=1):
        new_streak = current_streak + 1
    else:
        new_streak = 1
    cur.execute("UPDATE users SET last_activity_date=?, current_streak=? WHERE user_id=?", (today.isoformat(), new_streak, user_id))
    conn.commit()
    conn.close()

def get_today_tasks(user_id: int) -> List[Dict]:
    today_str = date.today().isoformat()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT task_id, task_text, completed FROM user_daily_tasks WHERE user_id=? AND task_date=?", (user_id, today_str))
    rows = cur.fetchall()
    if rows:
        tasks = [{"id": row[0], "text": row[1], "completed": bool(row[2])} for row in rows]
    else:
        cur.execute("SELECT id, text FROM tasks_template ORDER BY RANDOM() LIMIT 3")
        templates = cur.fetchall()
        tasks = []
        for task_id, task_text in templates:
            cur.execute("INSERT INTO user_daily_tasks (user_id, task_id, task_text, completed, task_date) VALUES (?,?,?,?,?)",
                        (user_id, task_id, task_text, False, today_str))
            tasks.append({"id": task_id, "text": task_text, "completed": False})
        conn.commit()
    conn.close()
    return tasks

def mark_task_complete(user_id: int, task_id: int):
    today_str = date.today().isoformat()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE user_daily_tasks SET completed=1 WHERE user_id=? AND task_id=? AND task_date=? AND completed=0",
                (user_id, task_id, today_str))
    if cur.rowcount > 0:
        cur.execute("UPDATE users SET total_completed = total_completed + 1 WHERE user_id=?", (user_id,))
        conn.commit()
        update_streak(user_id)
    conn.close()

def get_user_stats(user_id: int):
    today_str = date.today().isoformat()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM user_daily_tasks WHERE user_id=? AND task_date=? AND completed=1", (user_id, today_str))
    completed_today = cur.fetchone()[0]
    cur.execute("SELECT total_completed, current_streak FROM users WHERE user_id=?", (user_id,))
    total_all, streak = cur.fetchone()
    conn.close()
    return completed_today, total_all, streak

def reset_all_users_tasks():
    logging.info("Ежедневный сброс заданий")

# ========== КЛАВИАТУРЫ ==========
def get_tasks_keyboard(tasks: List[Dict]) -> InlineKeyboardMarkup:
    buttons = []
    for task in tasks:
        status = "✅" if task["completed"] else "⬜"
        if task["completed"]:
            buttons.append([InlineKeyboardButton(text=f"{status} {task['text']} (выполнено)", callback_data="noop")])
        else:
            buttons.append([InlineKeyboardButton(text=f"{status} {task['text']}", callback_data=f"complete_{task['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_main_menu_inline() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📋 Задания на сегодня", callback_data="menu_tasks")],
        [InlineKeyboardButton(text="📊 Моя статистика", callback_data="menu_stats")],
        [InlineKeyboardButton(text="🤖 Спросить AI (без истории)", callback_data="menu_ask")],
        [InlineKeyboardButton(text="💬 Диалог с AI (с памятью)", callback_data="menu_chat")],
        [InlineKeyboardButton(text="🧠 Выбрать модель AI", callback_data="menu_model")],
        [InlineKeyboardButton(text="🧹 Очистить историю диалога", callback_data="menu_clear")],
        [InlineKeyboardButton(text="🚪 Выйти из диалога", callback_data="menu_stop")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_model_selection_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    for model_id, model_name in AVAILABLE_MODELS.items():
        buttons.append([InlineKeyboardButton(text=model_name, callback_data=f"setmodel_{model_id}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад в меню", callback_data="menu_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ========== OPENROUTER ==========
async def get_ai_response(messages: list, model: str = None) -> str:
    if not OPENROUTER_API_KEY:
        return "⚠️ API-ключ OpenRouter не настроен."
    model_to_use = model or DEFAULT_MODEL
    try:
        response = await client.chat.completions.create(
            model=model_to_use,
            messages=messages,
            max_tokens=1000,
            temperature=0.7,
        )
        content = response.choices[0].message.content
        return content if content is not None else "🤖 Пустой ответ."
    except Exception as e:
        logging.error(f"OpenRouter ошибка: {e}")
        return f"❌ Ошибка: {e}"

async def send_long_text(message: Message, text: str, filename: str = "answer.txt"):
    if text is None:
        text = "Пустой ответ."
    if len(text) <= MAX_MESSAGE_LEN:
        await message.answer(text)
    else:
        file_bytes = text.encode('utf-8')
        file_io = BufferedInputFile(file_bytes, filename=filename)
        await message.answer_document(document=file_io, caption="📄 Ответ в файле.")

# ========== FSM ДИАЛОГ ==========
class ChatWithAI(StatesGroup):
    chatting = State()

@dp.message(Command("chat"))
async def start_chat(message: Message, state: FSMContext):
    if not OPENROUTER_API_KEY:
        await message.answer("❌ AI не настроен.")
        return
    await state.set_state(ChatWithAI.chatting)
    await state.update_data(history=[])
    await message.answer(
        "💬 Режим диалога включён.\nКоманды: /clear_context, /stop, /menu",
        reply_markup=get_main_keyboard()
    )

@dp.message(ChatWithAI.chatting)
async def chat_with_ai(message: Message, state: FSMContext):
    user_input = message.text.strip()
    if not user_input or user_input.startswith("/"):
        return
    data = await state.get_data()
    history = data.get("history", [])
    user_model = data.get("model", DEFAULT_MODEL)
    history.append({"role": "user", "content": user_input})
    await bot.send_chat_action(message.chat.id, action="typing")
    answer = await get_ai_response(history, model=user_model)
    history.append({"role": "assistant", "content": answer})
    await state.update_data(history=history)
    await send_long_text(message, answer, "reply.txt")

@dp.message(Command("clear_context"))
async def clear_context_command(message: Message, state: FSMContext):
    if await state.get_state() != ChatWithAI.chatting:
        await message.answer("Ты не в диалоге. /chat")
        return
    await state.update_data(history=[])
    await message.answer("🧹 История очищена.")

@dp.message(Command("stop"))
async def stop_chat_command(message: Message, state: FSMContext):
    if await state.get_state() == ChatWithAI.chatting:
        await state.clear()
        await message.answer("🔚 Диалог завершён.")
    else:
        await message.answer("Ты и так не в диалоге.")

# ========== ОСНОВНЫЕ КОМАНДЫ ==========
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    get_or_create_user(user_id)
    await message.answer(
        "Привет, любимая! 💕\nЯ бот, который помогает нам быть ближе.\n\n"
        "Кнопка «🔮 Меню» внизу или команды:\n"
        "/tasks, /stats, /ask, /chat, /model, /clear_context, /stop",
        reply_markup=get_main_keyboard()
    )

@dp.message(F.text == "🔮 Меню")
@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    await message.answer("🔮 Главное меню:", reply_markup=get_main_menu_inline())

@dp.message(Command("tasks"))
async def cmd_tasks(message: Message):
    user_id = message.from_user.id
    get_or_create_user(user_id)
    tasks = get_today_tasks(user_id)
    if not tasks:
        await message.answer("Заданий нет, ты супер!")
        return
    await message.answer("✨ Задания на сегодня:", reply_markup=get_tasks_keyboard(tasks))

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    user_id = message.from_user.id
    get_or_create_user(user_id)
    completed_today, total_all, streak = get_user_stats(user_id)
    await message.answer(
        f"📊 Статистика:\n✅ Сегодня: {completed_today}\n🏆 Всего: {total_all}\n🔥 Серия: {streak}\n\nТы умничка ❤️"
    )

@dp.message(Command("ask"))
async def ask_simple(message: Message, state: FSMContext):
    user_input = message.text.replace("/ask", "").strip()
    if not user_input:
        await message.answer("Пример: /ask Как сделать сюрприз?")
        return
    current_state = await state.get_state()
    if current_state == ChatWithAI.chatting:
        data = await state.get_data()
        user_model = data.get("model", DEFAULT_MODEL)
    else:
        user_model = DEFAULT_MODEL
    await bot.send_chat_action(message.chat.id, action="typing")
    answer = await get_ai_response([{"role": "user", "content": user_input}], model=user_model)
    await send_long_text(message, answer, "answer.txt")

@dp.message(Command("model"))
async def cmd_model(message: Message):
    await message.answer("🧠 Выбери модель AI:", reply_markup=get_model_selection_keyboard())

# ========== CALLBACKS ==========
@dp.callback_query(F.data.startswith("complete_"))
async def complete_task(callback: CallbackQuery):
    user_id = callback.from_user.id
    task_id = int(callback.data.split("_")[1])
    mark_task_complete(user_id, task_id)
    tasks = get_today_tasks(user_id)
    await callback.message.edit_text("✨ Задания обновлены:", reply_markup=get_tasks_keyboard(tasks))
    await callback.answer("Задание выполнено! 💖")

@dp.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer("Уже выполнено", show_alert=False)

@dp.callback_query(F.data.startswith("menu_"))
async def menu_callback(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split("_")[1]
    user_id = callback.from_user.id
    get_or_create_user(user_id)

    if action == "tasks":
        tasks = get_today_tasks(user_id)
        await callback.message.answer("✨ Задания:", reply_markup=get_tasks_keyboard(tasks) if tasks else "Нет заданий")
    elif action == "stats":
        c, t, s = get_user_stats(user_id)
        await callback.message.answer(f"📊 Статистика:\n✅ Сегодня: {c}\n🏆 Всего: {t}\n🔥 Серия: {s}")
    elif action == "ask":
        await callback.message.answer("Напиши /ask вопрос")
    elif action == "chat":
        if not OPENROUTER_API_KEY:
            await callback.message.answer("❌ AI не настроен")
        else:
            await state.set_state(ChatWithAI.chatting)
            await state.update_data(history=[])
            await callback.message.answer("💬 Диалог включён. /stop для выхода")
    elif action == "model":
        await callback.message.answer("🧠 Выбери модель:", reply_markup=get_model_selection_keyboard())
    elif action == "clear":
        if await state.get_state() == ChatWithAI.chatting:
            await state.update_data(history=[])
            await callback.message.answer("🧹 История очищена.")
        else:
            await callback.message.answer("Ты не в диалоге.")
    elif action == "stop":
        if await state.get_state() == ChatWithAI.chatting:
            await state.clear()
            await callback.message.answer("🔚 Диалог завершён.")
        else:
            await callback.message.answer("Диалог не активен.")
    elif action == "back":
        await callback.message.edit_text("🔮 Главное меню:", reply_markup=get_main_menu_inline())
    await callback.answer()

@dp.callback_query(F.data.startswith("setmodel_"))
async def set_model_callback(callback: CallbackQuery, state: FSMContext):
    model_id = callback.data.split("_", 1)[1]
    if model_id in AVAILABLE_MODELS:
        if await state.get_state() == ChatWithAI.chatting:
            await state.update_data(model=model_id)
            await callback.message.answer(f"✅ Модель: {AVAILABLE_MODELS[model_id]}")
        else:
            await callback.message.answer(f"✅ Модель выбрана: {AVAILABLE_MODELS[model_id]}\nНачни /chat")
        await callback.answer()
    else:
        await callback.answer("❌ Модель не найдена", show_alert=True)

# ========== ПЛАНИРОВЩИК ==========
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

async def on_startup():
    init_db()
    scheduler.add_job(reset_all_users_tasks, "cron", hour=RESET_HOUR, minute=RESET_MINUTE)
    scheduler.start()
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="menu", description="Открыть меню"),
        BotCommand(command="tasks", description="Задания на сегодня"),
        BotCommand(command="stats", description="Моя статистика"),
        BotCommand(command="ask", description="Спросить AI"),
        BotCommand(command="chat", description="Диалог с AI"),
        BotCommand(command="model", description="Выбрать модель AI"),
        BotCommand(command="clear_context", description="Очистить историю"),
        BotCommand(command="stop", description="Выйти из диалога"),
    ])
    logging.info("✅ Бот запущен")

async def on_shutdown():
    scheduler.shutdown()
    await bot.session.close()
    logging.info("👋 Бот остановлен")

async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
