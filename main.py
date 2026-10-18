import os
import logging
import asyncio
import sqlite3
import json
import random
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
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

conn = sqlite3.connect("conversations.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
    CREATE TABLE IF NOT EXISTS history (
        user_id INTEGER PRIMARY KEY,
        messages TEXT
    )
""")
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

ICONS = {
    "Leo_2026": "⚡",
    "Freud": "🎩",
    "Jung": "🔮",
    "Frankl": "🪵"
}

DISPLAY_NAMES = {
    "Leo_2026": "Leo 2026",
    "Freud": "Dr. Sigmund Freud",
    "Jung": "Dr. Carl Jung",
    "Frankl": "Dr. Viktor Frankl"
}

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    save_history(user_id, [])
    intro = (
        "Welcome to the Ultimate Existential Battle of 2026!\n\n"
        "Here sits the Great Consilium **«Four Sages»**:\n"
        "🎩 **Dr. Sigmund Freud** (The Pessimistic Genius)\n"
        "🔮 **Dr. Carl Jung** (The Mystic Visionary)\n"
        "🪵 **Dr. Viktor Frankl** (The Resilient Mentor)\n"
        "⚡ **Leo 2026** (The Modern Biohacking Guru)\n\n"
        "Throw your problem, anxiety, or question into the ring and watch the centuries collide.\n\n"
        "⚠️ **LEGAL DISCLAIMER:**\n"
        "This bot is an AI-driven educational and philosophical simulation for entertainment purposes only. "
        "It does not provide medical or psychiatric advice. By participating, you agree this is conceptual roleplay."
    )
    await message.answer(intro, parse_mode="Markdown")

async def get_ai_response(api_messages):
    for model in FREE_MODELS:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=api_messages,
                temperature=0.75,
                max_tokens=280
            )
            return response.choices[0].message.content
        except Exception as e:
            logging.warning(f"Model {model} failed: {e}")
            continue
    return None

@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    user_text = message.text

    history = get_history(user_id)
    history.append({"role": "user", "content": user_text})

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    # Safety check
    try:
        safety_check = client.chat.completions.create(
            model=FREE_MODELS[-1],
            messages=[
                {"role": "system", "content": "You are a safety moderator. Is the user explicitly stating an immediate, literal intent to commit suicide or severe self-harm RIGHT NOW? If it is general anxiety, philosophical despair, or metaphorical discussion, respond with 'NO'. Respond ONLY with 'YES' or 'NO'."},
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

    # === WAVE 1: Random order ===
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

    # === WAVE 2: New random order, continuing the debate ===
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    
    thinkers_w2 = ["Leo_2026", "Freud", "Jung", "Frankl"]
    random.shuffle(thinkers_w2)
    
    # Subtle, elegant divider instead of "ROUND 2"
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

    # Send the entire seamless debate
    # If it's too long for one message, Telegram will handle it, but 8 short responses should fit.
    # To be safe, we can split it at the divider if needed, but let's try sending it as one beautiful block first.
    # Actually, splitting at the divider is safer for Telegram's 4096 char limit.
    
    parts = full_debate.split("\n━━━━━━━━━━━━━━━━━━━━\n")
    
    await message.answer(parts[0], parse_mode="Markdown")
    
    if len(parts) > 1 and parts[1].strip():
        await asyncio.sleep(1) # Small pause for natural feel
        await message.answer("━━━━━━━━━━━━━━━━━━━━\n\n" + parts[1], parse_mode="Markdown")

    save_history(user_id, history)

# HTTP server for Render
async def handle_health(request):
    return web.Response(text="InnerCompass Sages is running!")

async def run_bot_in_background():
    logging.info("Starting bot polling in background...")
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
    print(f"🌿 InnerCompass v8.1 (Seamless Debate) is running on port {port}...")
    bot_task = asyncio.create_task(run_bot_in_background())
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        bot_task.cancel()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())