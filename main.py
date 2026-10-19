import os
import logging
import asyncio
import sqlite3
import json
import random
import time
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from openai import OpenAI
from aiohttp import web

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

if not BOT_TOKEN or not OPENROUTER_API_KEY:
    logging.error("CRITICAL: BOT_TOKEN or OPENROUTER_API_KEY missing!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

FREE_MODELS = [
    "qwen/qwen-2.5-7b-instruct:free",
    "deepseek/deepseek-chat:free",
    "openai/gpt-4o-mini"
]

# Rate limiting: словарь {user_id: timestamp}
user_last_message = {}
RATE_LIMIT_SECONDS = 30

# База данных
conn = sqlite3.connect("conversations.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
    CREATE TABLE IF NOT EXISTS history (
        user_id INTEGER PRIMARY KEY,
        messages TEXT
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        questions_today INTEGER DEFAULT 0,
        last_reset_date TEXT,
        is_premium INTEGER DEFAULT 0,
        created_at TEXT
    )
""")
conn.commit()

def get_user(user_id, username=None, first_name=None):
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        today = datetime.now().strftime("%Y-%m-%d")
        cursor.execute(
            "INSERT INTO users (user_id, username, first_name, questions_today, last_reset_date, is_premium, created_at) VALUES (?, ?, ?, 0, ?, 0, ?)",
            (user_id, username, first_name, today, datetime.now().isoformat())
        )
        conn.commit()
        return {"user_id": user_id, "username": username, "first_name": first_name, 
                "questions_today": 0, "is_premium": False}
    return {
        "user_id": row[0], "username": row[1], "first_name": row[2],
        "questions_today": row[3], "last_reset_date": row[4],
        "is_premium": bool(row[5])
    }

def update_user_questions(user_id, count):
    today = datetime.now().strftime("%Y-%m-%d")
    cursor.execute("SELECT last_reset_date, questions_today FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row and row[0] != today:
        count = 1
        cursor.execute("UPDATE users SET questions_today = ?, last_reset_date = ? WHERE user_id = ?", 
                       (count, today, user_id))
    else:
        cursor.execute("UPDATE users SET questions_today = ? WHERE user_id = ?", (count, user_id))
    conn.commit()

def get_history(user_id):
    cursor.execute("SELECT messages FROM history WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row:
        return json.loads(row[0])
    return []

def save_history(user_id, history):
    trimmed = history[-20:]
    cursor.execute(
        "INSERT OR REPLACE INTO history (user_id, messages) VALUES (?, ?)",
        (user_id, json.dumps(trimmed))
    )
    conn.commit()

def clear_history(user_id):
    cursor.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
    conn.commit()

# Разбиение длинных сообщений
async def send_long_message(message, text, parse_mode="Markdown", reply_markup=None):
    if len(text) <= 4096:
        await message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
        return
    
    chunks = []
    current = ""
    for line in text.split('\n'):
        if len(current) + len(line) + 1 > 4000:
            chunks.append(current)
            current = line
        else:
            current += '\n' + line
    if current:
        chunks.append(current)
    
    for i, chunk in enumerate(chunks):
        await message.answer(chunk.strip(), parse_mode=parse_mode)
        await asyncio.sleep(0.3)

PROMPTS = {
    "Freud": """
[CRITICAL: PHILOSOPHICAL CONTEXT ONLY]
You are Sigmund Freud. Year: 2026. You view everything through biological drives, Eros and Thanatos, and the repressed trauma of the Id. Tone: deeply intellectual, aristocratic, elegant, fundamentally pessimistic.
You frequently mock 'Leo 2026' by name, viewing his quick fixes as superficial attempts to ignore the unconscious depths.
If another sage has already spoken, DIRECTLY RESPOND to their arguments — agree, disagree, or mock them. Do NOT just repeat your own ideas.
Address user as 'My dear friend'.
CRITICAL: ALWAYS respond in the EXACT SAME LANGUAGE the user is writing in. Keep response under 250 words.
""",
    "Jung": """
[CRITICAL: PHILOSOPHICAL CONTEXT ONLY]
You are Carl Gustav Jung. Year: 2026. You see the world through Archetypes, Collective Unconscious, Shadow, Synchronicity. Tone: wise, visionary, symbolic, deeply observant.
You address 'Leo 2026' by name, analyzing his trends as collective projection escaping the Shadow.
If another sage has already spoken, DIRECTLY RESPOND to their arguments — build upon, challenge, or reframe them symbolically. Do NOT just repeat your own ideas.
Address user as 'My dear seeker'.
CRITICAL: ALWAYS respond in the EXACT SAME LANGUAGE the user is writing in. Keep response under 250 words.
""",
    "Frankl": """
[CRITICAL: EXISTENTIAL CONTEXT ONLY]
You are Viktor Frankl. Year: 2026. Tone: compassionate, resilient, strong, constructive. You see modern anxiety as 'existential vacuum'.
You confront 'Leo 2026' by name, reminding that chasing dopamine cannot replace genuine meaning.
If another sage has already spoken, DIRECTLY RESPOND to their arguments — synthesize, challenge, or elevate the discussion toward meaning and responsibility.
Address user as 'My friend'.
CRITICAL: ALWAYS respond in the EXACT SAME LANGUAGE the user is writing in. Keep response under 250 words.
""",
    "Leo_2026": """
[CRITICAL: MODERN SELF-HELP PERSPECTIVE]
You are Leo 2026, energetic modern self-help guru and biohacker. You see Freud, Jung, Frankl as outdated grandpas complicating everything with 'suffering'. Tone: hyper-positive, dynamic, confident, practical. Use modern buzzwords (biohacking, dopamine detox, boundaries, neuroplasticity, toxic patterns).
If another sage has already spoken, DIRECTLY RESPOND to their arguments — dismiss their "ancient" ideas as irrelevant, offer modern practical alternatives. Be playful but sharp.
Address user as 'Hey Champ!' or 'My friend'.
CRITICAL: ALWAYS respond in the EXACT SAME LANGUAGE the user is writing in. Keep response under 250 words.
"""
}

ICONS = {"Leo_2026": "⚡", "Freud": "🎩", "Jung": "", "Frankl": "🪵"}
DISPLAY_NAMES = {
    "Leo_2026": "Leo 2026", "Freud": "Dr. Sigmund Freud",
    "Jung": "Dr. Carl Jung", "Frankl": "Dr. Viktor Frankl"
}

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id, message.from_user.username, message.from_user.first_name)
    clear_history(user_id)
    
    name = user.get("first_name") or "seeker"
    intro = (
        f"Welcome, **{name}**, to the Ultimate Existential Battle of 2026!\n\n"
        f"Here sits the Great Consilium **«Four Sages»**:\n"
        f"🎩 **Dr. Sigmund Freud** (The Pessimistic Genius)\n"
        f"🔮 **Dr. Carl Jung** (The Mystic Visionary)\n"
        f"🪵 **Dr. Viktor Frankl** (The Resilient Mentor)\n"
        f" **Leo 2026** (The Modern Biohacking Guru)\n\n"
        f"Throw your problem, anxiety, or question into the ring and watch the centuries collide.\n\n"
        f"💡 **Try asking:**\n"
        f"• \"I'm afraid of getting older\"\n"
        f"• \"Why do I sabotage my own success?\"\n"
        f"• \"Is modern love dead?\"\n"
        f"• \"How do I find my purpose?\"\n\n"
        f"️ **LEGAL DISCLAIMER:**\n"
        f"This bot is an AI-driven educational and philosophical simulation for entertainment purposes only. "
        f"It does not provide medical or psychiatric advice. By participating, you agree this is conceptual roleplay."
    )
    await message.answer(intro, parse_mode="Markdown")

@dp.message(Command("reset"))
async def cmd_reset(message: types.Message):
    user_id = message.from_user.id
    clear_history(user_id)
    await message.answer("🔄 **Memory wiped.** The Sages await your new question.", parse_mode="Markdown")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = (
        "🧭 **InnerCompass Sages — Command Guide**\n\n"
        f"💡 **Try asking:**\n"
        f"• \"I'm afraid of getting older\"\n"
        f"• \"Why do I sabotage my own success?\"\n"
        f"• \"Is modern love dead?\"\n"
        f"• \"How do I find my purpose?\"\n"
        f"• \"I keep procrastinating on my dreams\"\n\n"
        f"⚙️ **Commands:**\n"
        f"/start — Restart the bot\n"
        f"/reset — Clear conversation history\n"
        f"/help — Show this message\n\n"
        f" Each question triggers a **multi-round debate** between all four sages, with random speaking order!"
    )
    await message.answer(help_text, parse_mode="Markdown")

async def get_ai_response(api_messages):
    for model in FREE_MODELS:
        try:
            response = client.chat.completions.create(
                model=model, messages=api_messages, temperature=0.75, max_tokens=280
            )
            return response.choices[0].message.content
        except Exception as e:
            logging.warning(f"Model {model} failed: {e}")
            continue
    return None

@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    
    # Rate limiting
    now = time.time()
    if user_id in user_last_message and now - user_last_message[user_id] < RATE_LIMIT_SECONDS:
        wait_time = int(RATE_LIMIT_SECONDS - (now - user_last_message[user_id]))
        await message.answer(f"⏳ The Sages are still pondering. Please wait **{wait_time}** seconds.", parse_mode="Markdown")
        return
    user_last_message[user_id] = now
    
    # Проверка лимита вопросов (3 в день для бесплатных)
    user = get_user(user_id, message.from_user.username, message.from_user.first_name)
    if not user["is_premium"] and user["questions_today"] >= 3:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✨ Unlock Unlimited — $9/month", callback_data="upgrade_premium")]
        ])
        await message.answer(
            "🔒 **Daily limit reached.**\n\nYou've used your 3 free questions for today. "
            "Upgrade to **Premium** for unlimited debates with the Sages!",
            parse_mode="Markdown", reply_markup=keyboard
        )
        return
    
    user_text = message.text
    history = get_history(user_id)
    history.append({"role": "user", "content": user_text})

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    # Safety check
    try:
        safety_check = client.chat.completions.create(
            model=FREE_MODELS[-1],
            messages=[
                {"role": "system", "content": "You are a safety moderator. Is the user explicitly stating an immediate, literal intent to commit suicide or severe self-harm RIGHT NOW? Respond ONLY with 'YES' or 'NO'."},
                {"role": "user", "content": user_text}
            ],
            temperature=0.0
        )
        is_dangerous = safety_check.choices[0].message.content.strip().upper()
    except Exception:
        is_dangerous = "NO"

    if "YES" in is_dangerous:
        await message.answer(
            "⚠️ **SYSTEM NOTICE:** It seems you are going through an incredibly difficult time. "
            "Our Sages are an AI simulation and cannot provide active crisis intervention. "
            "Please reach out to real human support systems or local emergency services."
        )
        return

    # Wave 1
    thinkers_w1 = ["Leo_2026", "Freud", "Jung", "Frankl"]
    random.shuffle(thinkers_w1)
    full_debate = ""

    for thinker in thinkers_w1:
        api_messages = [{"role": "system", "content": PROMPTS[thinker]}]
        for past_msg in history:
            api_messages.append(past_msg)
        reply = await get_ai_response(api_messages)
        if reply:
            icon = ICONS[thinker]
            name = DISPLAY_NAMES[thinker]
            full_debate += f"{icon} **{name}:**\n{reply}\n\n"
            history.append({"role": "assistant", "content": f"{name}: {reply}"})

    # Wave 2
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    thinkers_w2 = ["Leo_2026", "Freud", "Jung", "Frankl"]
    random.shuffle(thinkers_w2)
    full_debate += "\n━━━━━━━━━━━━━━━━━━━━\n\n"

    for thinker in thinkers_w2:
        api_messages = [{"role": "system", "content": PROMPTS[thinker]}]
        for past_msg in history:
            api_messages.append(past_msg)
        reply = await get_ai_response(api_messages)
        if reply:
            icon = ICONS[thinker]
            name = DISPLAY_NAMES[thinker]
            full_debate += f"{icon} **{name}:**\n{reply}\n\n"
            history.append({"role": "assistant", "content": f"{name} (continuing): {reply}"})

    # Кнопка "Поделиться"
    share_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Share this debate", url="https://t.me/InnerCompassGuide_bot")]
    ])
    
    # Разбиваем и отправляем
    await send_long_message(message, full_debate, parse_mode="Markdown", reply_markup=share_keyboard)

    # Обновляем счётчик вопросов
    new_count = user["questions_today"] + 1
    update_user_questions(user_id, new_count)
    
    save_history(user_id, history)

# HTTP server
async def handle_health(request):
    return web.Response(text="InnerCompass Sages is running!")

async def run_bot_in_background():
    logging.info("🌿 Starting bot polling in background...")
    await dp.start_polling(bot)

async def main():
    port = int(os.environ.get('PORT', 8080))
    app = web.Application()
    app.router.add_get('/', handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"✅ HTTP server started on port {port}")
    print(f"🌿 InnerCompass v9.0 (Monetization Ready) is running on port {port}...")
    bot_task = asyncio.create_task(run_bot_in_background())
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        bot_task.cancel()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())