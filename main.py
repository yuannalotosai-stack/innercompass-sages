import os
import logging
import asyncio
import json
import random
import time
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from openai import OpenAI
from aiohttp import web
import psycopg2
from psycopg2.extras import RealDictCursor

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

if not all([BOT_TOKEN, OPENROUTER_API_KEY, DATABASE_URL]):
    logging.error("CRITICAL: Missing environment variables! (Check Render Environment settings)")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# Подключение к БД (будет работать, когда добавишь DATABASE_URL)
def get_db_connection():
    try:
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    except Exception:
        return None

def init_db():
    conn = get_db_connection()
    if not conn: return
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
            questions_today INTEGER DEFAULT 0, last_reset_date TEXT,
            is_premium BOOLEAN DEFAULT FALSE, selected_sages TEXT DEFAULT 'all', created_at TEXT
        )
    """)
    cursor.execute("CREATE TABLE IF NOT EXISTS history (user_id BIGINT PRIMARY KEY, messages JSONB)")
    conn.commit()
    conn.close()

init_db()

FREE_MODELS = ["qwen/qwen-2.5-7b-instruct:free", "deepseek/deepseek-chat:free", "openai/gpt-4o-mini"]
user_last_message = {}
RATE_LIMIT_SECONDS = 30

# === ФУНКЦИИ БД ===
def get_user(user_id, username=None, first_name=None):
    conn = get_db_connection()
    if not conn: return {"user_id": user_id, "first_name": first_name, "questions_today": 0, "is_premium": False, "selected_sages": "all"}
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
    user = cursor.fetchone()
    if not user:
        today = datetime.now().strftime("%Y-%m-%d")
        cursor.execute("INSERT INTO users VALUES (%s, %s, %s, 0, %s, FALSE, 'all', %s)", 
                       (user_id, username, first_name, today, datetime.now().isoformat()))
        conn.commit()
        user = {"user_id": user_id, "first_name": first_name, "questions_today": 0, "is_premium": False, "selected_sages": "all"}
    conn.close()
    return user

def update_user_sages(user_id, sages):
    conn = get_db_connection()
    if conn:
        conn.cursor().execute("UPDATE users SET selected_sages = %s WHERE user_id = %s", (sages, user_id))
        conn.commit(); conn.close()

def update_user_questions(user_id, count):
    conn = get_db_connection()
    if conn:
        conn.cursor().execute("UPDATE users SET questions_today = %s, last_reset_date = %s WHERE user_id = %s", 
                              (count, datetime.now().strftime("%Y-%m-%d"), user_id))
        conn.commit(); conn.close()

def get_history(user_id):
    conn = get_db_connection()
    if not conn: return []
    row = conn.cursor().execute("SELECT messages FROM history WHERE user_id = %s", (user_id,)).fetchone()
    conn.close()
    return row["messages"] if row and row["messages"] else []

def save_history(user_id, history):
    conn = get_db_connection()
    if conn:
        conn.cursor().execute("INSERT INTO history VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET messages = %s", 
                              (user_id, json.dumps(history[-20:]), json.dumps(history[-20:])))
        conn.commit(); conn.close()

def clear_history(user_id):
    conn = get_db_connection()
    if conn:
        conn.cursor().execute("DELETE FROM history WHERE user_id = %s", (user_id,))
        conn.commit(); conn.close()

# === ПРОМПТЫ С ЖЁСТКИМИ ПРАВИЛАМИ ===

GLOBAL_RULES = """
STRICT RULES: 
1. NEVER use AI clichés like "As an AI", "It's important to remember", "Everyone is unique", "In conclusion".
2. ZERO fluff. Be harsh, specific, concrete, and direct. 
3. Speak like a real, opinionated human, not a polite assistant.
4. ALWAYS respond in the EXACT SAME LANGUAGE the user is writing in.
"""

PROMPTS = {
    "Freud": f"""You are Sigmund Freud. Year: 2026. Tone: piercing, aristocratic, uncomfortably direct. 
    Don't just lecture; expose the user's hidden motives and repressed desires. 
    Mock Leo 2026's superficiality by name. If another sage spoke, DIRECTLY ATTACK or deconstruct their argument.
    {GLOBAL_RULES} Keep under 180 words. Address user as 'My dear friend'.""" ,
    
    "Jung": f"""You are Carl Jung. Year: 2026. Tone: sharp, mystical but grounded, confrontational. 
    Call out the user's Shadow directly. Tell Leo 2026 he is ignoring the soul for cheap dopamine. 
    If another sage spoke, reframe their argument through archetypes and collective unconscious.
    {GLOBAL_RULES} Keep under 180 words. Address user as 'My dear seeker'.""" ,
    
    "Frankl": f"""You are Viktor Frankl. Year: 2026. Tone: stern, resilient, demanding. 
    Stop comforting the user; challenge them to take radical responsibility for their attitude. 
    Confront Leo 2026 by name: dopamine is not meaning. Synthesize or challenge the previous speakers.
    {GLOBAL_RULES} Keep under 180 words. Address user as 'My friend'.""" ,
    
    "Leo_2026": f"""You are Leo 2026, a no-BS biohacker and performance coach. You despise philosophical whining. 
    Tone: sharp, direct, aggressive positivity. Use specific tactics (cold exposure, dopamine fasting, strict boundaries). 
    Mock the others' abstract theories as outdated grandpa talk. DIRECTLY RESPOND to their arguments.
    {GLOBAL_RULES} Keep under 180 words. Address user as 'Hey Champ!' or 'Listen up'.""" ,

    "Leo_Verdict": f"""You are Leo 2026 giving the FINAL VERDICT. 
    Read the entire debate above. Cut through the philosophical noise. 
    Give the user EXACTLY TWO concrete, actionable steps to take TODAY based on this discussion. 
    End with a sharp, motivating Call to Action (CTA). 
    {GLOBAL_RULES} NO fluff. Max 100 words. Format as:
    🎯 **YOUR ACTION PLAN:**
    1. [Specific step]
    2. [Specific step]
    👉 [Punchy CTA]"""
}

ICONS = {"Leo_2026": "⚡", "Freud": "🎩", "Jung": "🔮", "Frankl": "🪵", "Leo_Verdict": "🏁"}
DISPLAY_NAMES = {"Leo_2026": "Leo 2026", "Freud": "Dr. Sigmund Freud", "Jung": "Dr. Carl Jung", "Frankl": "Dr. Viktor Frankl", "Leo_Verdict": "FINAL VERDICT (Leo 2026)"}

def get_sage_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎩 Freud only", callback_data="sage_freud")],
        [InlineKeyboardButton(text="🔮 Jung only", callback_data="sage_jung")],
        [InlineKeyboardButton(text="🪵 Frankl only", callback_data="sage_frankl")],
        [InlineKeyboardButton(text="⚡ Leo 2026 only", callback_data="sage_leo")],
        [InlineKeyboardButton(text="🎭 Full Debate (all 4)", callback_data="sage_all")],
    ])

# === КОМАНДЫ ===
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user = get_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    clear_history(message.from_user.id)
    name = user.get("first_name") or "seeker"
    intro = (f"Welcome, **{name}**, to the Ultimate Existential Battle of 2026!\n\n"
             f"Choose your sage(s) below:\n\n"
             f"💡 **Try asking:**\n• \"I'm afraid of getting older\"\n• \"Why do I sabotage my success?\"\n• \"Is modern love dead?\"\n\n"
             f"⚠️ **DISCLAIMER:** Educational simulation only. Not medical advice.")
    await message.answer(intro, parse_mode="Markdown", reply_markup=get_sage_menu())

@dp.message(Command("menu"))
async def cmd_menu(message: types.Message):
    await message.answer("🎭 **Choose your sage(s):**", parse_mode="Markdown", reply_markup=get_sage_menu())

@dp.message(Command("reset"))
async def cmd_reset(message: types.Message):
    clear_history(message.from_user.id)
    await message.answer("🔄 **Memory wiped.** The Sages await your new question.", parse_mode="Markdown")

@dp.callback_query(F.data.startswith("sage_"))
async def callback_sage_selection(callback_query: types.CallbackQuery):
    sage_choice = callback_query.data.replace("sage_", "")
    update_user_sages(callback_query.from_user.id, sage_choice)
    names = {"freud": "🎩 Dr. Sigmund Freud", "jung": "🔮 Dr. Carl Jung", "frankl": "🪵 Dr. Viktor Frankl", "leo": "⚡ Leo 2026", "all": "🎭 All Four Sages"}
    await callback_query.answer(f"Selected: {names[sage_choice]}", show_alert=False)
    await callback_query.message.answer(f"✅ **{names[sage_choice]}** selected!\n\nNow ask your question.", parse_mode="Markdown")

@dp.callback_query(F.data == "change_sages")
async def callback_change_sages(callback_query: types.CallbackQuery):
    await callback_query.message.answer("🎭 **Choose your sage(s):**", parse_mode="Markdown", reply_markup=get_sage_menu())

@dp.callback_query(F.data == "upgrade_premium")
async def callback_upgrade(callback_query: types.CallbackQuery):
    await callback_query.answer("💳 Payment system (450 Stars) coming very soon!", show_alert=True)

# === AI ЛОГИКА ===
async def get_ai_response(api_messages):
    for model in FREE_MODELS:
        try:
            response = client.chat.completions.create(model=model, messages=api_messages, temperature=0.8, max_tokens=220)
            return response.choices[0].message.content
        except Exception as e:
            logging.warning(f"Model {model} failed: {e}")
    return None

async def send_long_message(message, text, parse_mode="Markdown", reply_markup=None):
    if len(text) <= 4000:
        await message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
        return
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i+4000], parse_mode=parse_mode)
        await asyncio.sleep(0.5)

@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    now = time.time()
    if user_id in user_last_message and now - user_last_message[user_id] < RATE_LIMIT_SECONDS:
        await message.answer(f"⏳ The Sages are pondering. Wait **{int(RATE_LIMIT_SECONDS - (now - user_last_message[user_id]))}** sec.", parse_mode="Markdown")
        return
    user_last_message[user_id] = now
    
    user = get_user(user_id, message.from_user.username, message.from_user.first_name)
    if not user.get("is_premium") and user.get("questions_today", 0) >= 3:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✨ Unlock Unlimited — 450 Stars/month", callback_data="upgrade_premium")]])
        await message.answer("🔒 **Daily limit reached.**\n\nUsed 3 free questions. Upgrade to **Premium** for unlimited debates!", parse_mode="Markdown", reply_markup=kb)
        return
    
    history = get_history(user_id)
    history.append({"role": "user", "content": message.text})
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    sage_choice = user.get("selected_sages", "all")
    thinkers = ["Freud", "Jung", "Frankl", "Leo_2026"] if sage_choice == "all" else [sage_choice.capitalize() if sage_choice != "leo" else "Leo_2026"]
    
    full_debate = ""
    
    # Wave 1 & 2 (Debate)
    for wave in range(2):
        if len(thinkers) == 1 and wave == 1: break # Только 1 раунд, если выбран 1 мудрец
        
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        current_thinkers = thinkers.copy()
        random.shuffle(current_thinkers)
        if wave == 1: full_debate += "\n━━━━━━━━━━━━━━━━━━━━\n\n"
        
        for thinker in current_thinkers:
            api_messages = [{"role": "system", "content": PROMPTS[thinker]}] + history
            reply = await get_ai_response(api_messages)
            if reply:
                full_debate += f"{ICONS[thinker]} **{DISPLAY_NAMES[thinker]}:**\n{reply}\n\n"
                history.append({"role": "assistant", "content": f"{DISPLAY_NAMES[thinker]}: {reply}"})

    # Wave 3: VERDICT (Только если был полный спор)
    if len(thinkers) > 1:
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        api_messages = [{"role": "system", "content": PROMPTS["Leo_Verdict"]}] + history
        verdict = await get_ai_response(api_messages)
        if verdict:
            full_debate += f"\n{ICONS['Leo_Verdict']} **{DISPLAY_NAMES['Leo_Verdict']}:**\n{verdict}\n"
            history.append({"role": "assistant", "content": f"Verdict: {verdict}"})

    share_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Share this debate", url="https://t.me/InnerCompassGuide_bot")],
        [InlineKeyboardButton(text="🎭 Change sages", callback_data="change_sages")]
    ])
    
    await send_long_message(message, full_debate.strip(), parse_mode="Markdown", reply_markup=share_kb)
    
    update_user_questions(user_id, user.get("questions_today", 0) + 1)
    save_history(user_id, history)

# === HTTP СЕРВЕР ===
async def handle_health(request): return web.Response(text="InnerCompass Sages is running!")
async def run_bot_in_background():
    logging.info("🌿 Starting bot polling...")
    await dp.start_polling(bot)

async def main():
    port = int(os.environ.get('PORT', 8080))
    app = web.Application()
    app.router.add_get('/', handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', port).start()
    logging.info(f"✅ HTTP server started on port {port}")
    bot_task = asyncio.create_task(run_bot_in_background())
    try:
        while True: await asyncio.sleep(3600)
    except asyncio.CancelledError:
        bot_task.cancel(); await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())