import asyncio
import html
import logging
import math
import os
import random
import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# ==================== SOZLAMALAR ====================
TOKEN = "9055733774:AAHIr8SxoXOTmz_nECP6VAIS8TtuKMmQI7U"
CHANNEL_ID = "@testbot_muazzamxon"
DB_NAME = "tests_rasch.db"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ==================== RASCH MODELI HISOBI ====================
def calculate_rasch(user_responses: list[int], difficulties: list[float]):
    total_items = len(user_responses)
    raw_score = sum(user_responses)

    if raw_score == 0:
        theta = min(difficulties) - 1.5
    elif raw_score == total_items:
        theta = max(difficulties) + 1.5
    else:
        theta = 0.0
        for _ in range(25):
            # math.exp toshishining (overflow) oldini olish
            p = []
            for b in difficulties:
                diff_exp = -(theta - b)
                diff_exp = max(-500.0, min(500.0, diff_exp))
                p.append(1.0 / (1.0 + math.exp(diff_exp)))

            f_val = raw_score - sum(p)
            f_prime = -sum(pi * (1.0 - pi) for pi in p)

            if abs(f_prime) < 1e-7:
                break

            delta = f_val / f_prime
            # Haddan tashqari katta qadamning oldini olish
            delta = max(-3.0, min(3.0, delta))
            theta -= delta
            if abs(delta) < 0.001:
                break

    theta = max(-6.0, min(6.0, theta))
    scaled_score = max(0.0, min(100.0, (theta + 3.0) / 6.0 * 100.0))
    scaled_score = round(scaled_score, 1)

    if scaled_score >= 85:
        grade = "A+"
    elif scaled_score >= 70:
        grade = "A"
    elif scaled_score >= 60:
        grade = "B+"
    elif scaled_score >= 50:
        grade = "B"
    elif scaled_score >= 46:
        grade = "C"
    else:
        grade = "Yiqildi"

    return round(theta, 2), scaled_score, grade


# ==================== MA'LUMOTLAR BAZASI ====================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE,
                creator_id INTEGER,
                photo_id TEXT,
                answers TEXT,
                difficulties TEXT,
                content_type TEXT DEFAULT 'photo',
                content_text TEXT DEFAULT ''
            )
        """)
        # Eski bazaga yangi ustunlar qo'shish (migratsiya)
        async with db.execute("PRAGMA table_info(tests)") as cursor:
            cols = [row[1] for row in await cursor.fetchall()]
            if "content_type" not in cols:
                await db.execute("ALTER TABLE tests ADD COLUMN content_type TEXT DEFAULT 'photo'")
            if "content_text" not in cols:
                await db.execute("ALTER TABLE tests ADD COLUMN content_text TEXT DEFAULT ''")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_code TEXT,
                user_id INTEGER,
                user_name TEXT,
                score REAL,
                theta REAL,
                correct_count INTEGER,
                total_q INTEGER,
                grade TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tests_code ON tests(code)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_results_test ON results(test_code, score)")
        await db.commit()


# ==================== FSM HOLATLARI ====================
class CreateTest(StatesGroup):
    waiting_for_material = State()
    waiting_for_answers = State()
    waiting_for_diff = State()


class SolveTest(StatesGroup):
    waiting_for_id = State()
    answering = State()


class CheckAnswersAndRank(StatesGroup):
    waiting_for_id = State()


# ==================== KLAVIATURALAR ====================
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📝 Test yechish")],
        [
            KeyboardButton(text="➕ Test qo'shish"),
            KeyboardButton(text="📊 Javoblar va o‘rinlar"),
        ],
    ],
    resize_keyboard=True,
)

cancel_menu = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Bekor qilish")]],
    resize_keyboard=True,
)

diff_options_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="⚡️ Standart taqsimot (Oson -> O'rtacha -> Qiyin)")],
        [KeyboardButton(text="✍️ O'zim kiritaman")],
        [KeyboardButton(text="❌ Bekor qilish")],
    ],
    resize_keyboard=True,
)


def get_options_keyboard(current_q: int, total_q: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="A", callback_data="ans_A"),
                InlineKeyboardButton(text="B", callback_data="ans_B"),
                InlineKeyboardButton(text="C", callback_data="ans_C"),
                InlineKeyboardButton(text="D", callback_data="ans_D"),
            ],
            [
                InlineKeyboardButton(text="❌ Testni to‘xtatish", callback_data="ans_cancel"),
            ],
        ]
    )


# ==================== /START VA BEKOR QILISH ====================
@dp.message(CommandStart())
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    user_name = html.escape(message.from_user.full_name or "Foydalanuvchi")

    welcome_text = (
        f"👋 <b>Assalomu alaykum, {user_name}!</b>\n\n"
        f"🎯 <b>Rasch modeli</b> asosidagi intellektual test tizimiga xush kelibsiz!\n\n"
        f"Bu bot orqali siz testlarni topshirishingiz, o‘z bilimingiz va qobiliyatingizni (Theta) "
        f"xalqaro ilmiy <b>Rasch modeli</b> orqali o‘ta aniqlikda baholashingiz hamda yangi testlar yaratishingiz mumkin.\n\n"
        f"⚠️ <b>MUHIM OGOHLANTIRISH VA QOIDALAR:</b>\n"
        f"──────────────────────────\n"
        f"1️⃣ <b>Diqqat bilan ishlang:</b> Testni boshlaganingizdan so‘ng belgilangan javob qayta o‘zgartirilmaydi.\n"
        f"2️⃣ <b>Adolatli ball:</b> Rasch modelida har bir savolning qiyinlik darajasi inobatga olinadi va yakuniy ball 100 ballik shkalada hisoblanadi.\n"
        f"3️⃣ <b>Shaffof natija:</b> Test tugagach, to‘plangan ball, qobiliyat ko‘rsatkichi va o‘quvchilar orasidagi o‘rningiz darhol e’lon qilinadi.\n"
        f"4️⃣ <b>To‘xtatish imkoni:</b> Zarurat tug‘ilganda testni istalgan payt '❌ Testni to‘xtatish' tugmasi orqali to‘xtatishingiz mumkin.\n"
        f"──────────────────────────\n\n"
        f"🚀 <i>Boshlash uchun quyidagi bo‘limlardan birini tanlang:</i>"
    )

    await message.answer(
        welcome_text,
        reply_markup=main_menu,
        parse_mode="HTML",
    )


@dp.message(F.text == "❌ Bekor qilish")
async def cancel_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Amal bekor qilindi. Asosiy menyu:", reply_markup=main_menu)


# ==================== TEST QO'SHISH ====================
@dp.message(F.text == "➕ Test qo'shish")
async def start_add_test(message: types.Message, state: FSMContext):
    await state.clear()
    await state.set_state(CreateTest.waiting_for_material)
    await message.answer(
        "📤 <b>Test savollarini yuboring.</b>\n\n"
        "Siz quyidagi usullardan birida yuborishingiz mumkin:\n"
        "• 📸 <b>Rasm</b> ko‘rinishida\n"
        "• 📄 <b>PDF yoki Hujjat</b> (.pdf, .docx va h.k.)\n"
        "• ✍️ <b>Matn (yozma)</b> ko‘rinishida to‘g‘ridan-to‘g‘ri yozib yuborishingiz mumkin.\n\n"
        "<i>Bekor qilish uchun '❌ Bekor qilish' tugmasini bosing.</i>",
        reply_markup=cancel_menu,
        parse_mode="HTML",
    )


@dp.message(CreateTest.waiting_for_material)
async def get_test_material(message: types.Message, state: FSMContext):
    if message.text == "❌ Bekor qilish":
        await cancel_handler(message, state)
        return

    content_type = None
    file_id = ""
    content_text = ""

    if message.photo:
        content_type = "photo"
        file_id = message.photo[-1].file_id
        content_text = message.caption or ""
    elif message.document:
        content_type = "document"
        file_id = message.document.file_id
        content_text = message.caption or message.document.file_name or ""
    elif message.text:
        content_type = "text"
        content_text = message.text
    else:
        await message.answer("Iltimos, rasm, PDF/hujjat yoki matn yuboring:")
        return

    await state.update_data(
        content_type=content_type,
        file_id=file_id,
        content_text=content_text,
    )
    await state.set_state(CreateTest.waiting_for_answers)
    await message.answer(
        "✅ <b>Test materiali qabul qilindi.</b>\n\n"
        "Endi to‘g‘ri javoblar ketma-ketligini kiriting (masalan: <b>ABCDABCD</b>):",
        reply_markup=cancel_menu,
        parse_mode="HTML",
    )


@dp.message(CreateTest.waiting_for_answers, F.text)
async def get_test_answers(message: types.Message, state: FSMContext):
    if message.text == "❌ Bekor qilish":
        await cancel_handler(message, state)
        return

    answers = message.text.strip().upper()
    if not answers or not all(ch in {"A", "B", "C", "D"} for ch in answers):
        await message.answer(
            "⚠️ Faqat A, B, C, D harflarini kiriting (masalan: <b>ABCD...</b>)!\n"
            "Qaytadan kiriting:",
            parse_mode="HTML",
        )
        return

    await state.update_data(answers=answers)
    await state.set_state(CreateTest.waiting_for_diff)
    await message.answer(
        f"Jami <b>{len(answers)} ta</b> savol qabul qilindi.\n\n"
        "Savollar qiyinlik darajasini qanday belgilaymiz?",
        reply_markup=diff_options_menu,
        parse_mode="HTML",
    )


@dp.message(CreateTest.waiting_for_diff)
async def get_test_difficulties(message: types.Message, state: FSMContext):
    if message.text == "❌ Bekor qilish":
        await cancel_handler(message, state)
        return

    data = await state.get_data()
    n = len(data["answers"])

    if message.text and message.text.startswith("⚡️ Standart"):
        diffs = [-1.5 + (3.0 * i / max(1, n - 1)) for i in range(n)]
    elif message.text and message.text.startswith("✍️"):
        await message.answer(
            f"Har bir savol qiyinligini (1 - Oson, 2 - O‘rtacha, 3 - Qiyin) vergul bilan yozing.\n"
            f"Masalan ({n} ta savol uchun): " + ",".join(["2"] * n) + "\n\n"
            f"<i>Jami {n} ta raqam bo'lishi kerak.</i>",
            parse_mode="HTML",
        )
        return
    else:
        raw_vals = [x.strip() for x in (message.text or "").split(",") if x.strip()]
        if len(raw_vals) != n:
            await message.answer(
                f"⚠️ Savollar soni {n} ta bo'lishi kerak! Siz {len(raw_vals)} ta kiritdingiz.\n"
                f"Qaytadan kiriting:"
            )
            return

        if not all(v in {"1", "2", "3"} for v in raw_vals):
            await message.answer(
                "⚠️ Faqat 1, 2 yoki 3 raqamlaridan foydalaning (1 - Oson, 2 - O‘rtacha, 3 - Qiyin)!\n"
                f"Masalan: " + ",".join(["2"] * n)
            )
            return

        mapping = {"1": -1.2, "2": 0.0, "3": 1.2}
        diffs = [mapping[val] for val in raw_vals]

    diff_str = ",".join(str(round(d, 3)) for d in diffs)

    # Unikal Test ID generatsiya qilish
    async with aiosqlite.connect(DB_NAME) as db:
        while True:
            test_code = str(random.randint(10000, 99999))
            async with db.execute("SELECT 1 FROM tests WHERE code = ?", (test_code,)) as cursor:
                if not await cursor.fetchone():
                    break

        await db.execute(
            """
            INSERT INTO tests (code, creator_id, photo_id, answers, difficulties, content_type, content_text)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                test_code,
                message.from_user.id,
                data.get("file_id", ""),
                data["answers"],
                diff_str,
                data.get("content_type", "photo"),
                data.get("content_text", ""),
            ),
        )
        await db.commit()

    await state.clear()
    await message.answer(
        f"✅ <b>Test muvaffaqiyatli saqlandi!</b>\n\n"
        f"🔑 <b>Test ID:</b> <code>{test_code}</code>\n"
        f"📊 Savollar soni: {n} ta\n\n"
        f"O‘quvchilarga ushbu ID raqamni bering.",
        reply_markup=main_menu,
        parse_mode="HTML",
    )


# ==================== TEST YECHISH ====================
@dp.message(F.text == "📝 Test yechish")
async def start_solve_test(message: types.Message, state: FSMContext):
    await state.clear()
    await state.set_state(SolveTest.waiting_for_id)
    await message.answer(
        "Test ID raqamini kiriting:\n\n<i>Bekor qilish uchun '❌ Bekor qilish' ni bosing.</i>",
        reply_markup=cancel_menu,
        parse_mode="HTML",
    )


@dp.message(SolveTest.waiting_for_id, F.text)
async def enter_test_id(message: types.Message, state: FSMContext):
    if message.text == "❌ Bekor qilish":
        await cancel_handler(message, state)
        return

    test_code = message.text.strip()

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            """
            SELECT photo_id, answers, difficulties, content_type, content_text
            FROM tests WHERE code = ?
            """,
            (test_code,),
        ) as cursor:
            row = await cursor.fetchone()

    if not row:
        await message.answer("⚠️ Bunday ID li test topilmadi. Qaytadan tekshirib kiriting:")
        return

    photo_id, answers, diff_str, content_type, content_text = row
    content_type = content_type or "photo"
    content_text = content_text or ""
    diffs = [float(x) for x in diff_str.split(",")]
    total_q = len(answers)

    await state.set_state(SolveTest.answering)
    await state.update_data(
        test_code=test_code,
        correct_answers=answers,
        difficulties=diffs,
        user_answers=[],
        current_q=1,
        total_q=total_q,
    )

    caption_text = (
        f"📋 <b>Test ID:</b> <code>{test_code}</code>\n"
        f"📊 Jami savollar: {total_q} ta.\n\n"
        f"Quyidagi variantlardan javoblarni belgilang:"
    )

    # 1. Avval testni (rasm, PDF yoki yozma matn) to'liq chiqarib beramiz
    if content_type == "photo" and photo_id:
        await message.answer_photo(
            photo=photo_id,
            caption=caption_text,
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="HTML",
        )
    elif content_type == "document" and photo_id:
        await message.answer_document(
            document=photo_id,
            caption=caption_text,
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="HTML",
        )
    elif content_type == "text" or content_text:
        # Yozma test bo'lsa, avval test savollarini to'liq yozib beradi
        safe_content = html.escape(content_text)
        # Agar matn juda uzun bo'lsa, xatolik bermasligi uchun qismlarga bo'lamiz
        if len(safe_content) > 3800:
            chunks = [safe_content[i : i + 3800] for i in range(0, len(safe_content), 3800)]
            for chunk in chunks:
                await message.answer(chunk, reply_markup=ReplyKeyboardRemove())
            await message.answer(caption_text, parse_mode="HTML")
        else:
            await message.answer(
                f"📝 <b>Test savollari:</b>\n\n{safe_content}\n\n{caption_text}",
                reply_markup=ReplyKeyboardRemove(),
                parse_mode="HTML",
            )
    else:
        await message.answer(caption_text, reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")

    # 2. Test tagiga birinchi savol va javob variantlarini chiqaramiz
    await message.answer(
        f"<b>1-savol</b> (jami {total_q} ta):",
        reply_markup=get_options_keyboard(current_q=1, total_q=total_q),
        parse_mode="HTML",
    )


@dp.callback_query(SolveTest.answering, F.data.startswith("ans_"))
async def handle_answer_choice(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()

    data = await state.get_data()
    if not data or "current_q" not in data:
        await callback.message.edit_text("⚠️ Test muddati tugagan yoki bekor qilingan.")
        await callback.message.answer("Asosiy menyu:", reply_markup=main_menu)
        return

    action = callback.data.split("_")[1]

    # Testni to'xtatish tanlansa
    if action == "cancel":
        await state.clear()
        try:
            await callback.message.edit_text("❌ Test yechish to‘xtatildi.")
        except TelegramBadRequest:
            pass
        await callback.message.answer("Asosiy menyuga qaytdingiz:", reply_markup=main_menu)
        return

    user_answers = list(data.get("user_answers", []))
    user_answers.append(action)

    current_q = data["current_q"] + 1
    total_q = data["total_q"]
    correct_answers = data["correct_answers"]
    difficulties = data["difficulties"]
    test_code = data["test_code"]

    if current_q <= total_q:
        await state.update_data(user_answers=user_answers, current_q=current_q)
        try:
            await callback.message.edit_text(
                f"<b>{current_q}-savol</b> (jami {total_q} ta):",
                reply_markup=get_options_keyboard(current_q=current_q, total_q=total_q),
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            pass
    else:
        # Barcha savollar yechildi - hisob-kitob
        responses_binary = [1 if u == c else 0 for u, c in zip(user_answers, correct_answers)]
        correct_count = sum(responses_binary)
        wrong_count = total_q - correct_count
        theta, scaled_score, grade = calculate_rasch(responses_binary, difficulties)

        raw_user_name = callback.from_user.full_name or "Foydalanuvchi"
        safe_user_name = html.escape(raw_user_name)

        # Bazaga saqlash va haqiqiy unikal o'rinni aniqlash
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                """
                INSERT INTO results (test_code, user_id, user_name, score, theta, correct_count, total_q, grade)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    test_code,
                    callback.from_user.id,
                    raw_user_name,
                    scaled_score,
                    theta,
                    correct_count,
                    total_q,
                    grade,
                ),
            )
            await db.commit()

            # Har bir o'quvchining eng yaxshi bali bo'yicha unikal o'rin
            async with db.execute(
                """
                SELECT COUNT(*) + 1 FROM (
                    SELECT user_id, MAX(score) as best_score
                    FROM results
                    WHERE test_code = ?
                    GROUP BY user_id
                ) WHERE best_score > ?
                """,
                (test_code, scaled_score),
            ) as cursor:
                rank_row = await cursor.fetchone()
                user_rank = rank_row[0] if rank_row else 1

        rank_badge = {1: "🥇 1-o‘rin", 2: "🥈 2-o‘rin", 3: "🥉 3-o‘rin"}.get(
            user_rank, f"🎖 {user_rank}-o‘rin"
        )

        result_text = (
            f"🏁 <b>Test yakunlandi!</b>\n\n"
            f"👤 <b>O‘quvchi:</b> {safe_user_name}\n"
            f"🏆 <b>Egallagan o‘rningiz:</b> <b>{rank_badge}</b>\n"
            f"🎯 <b>Rasch balli:</b> <b>{scaled_score} / 100</b> ({grade})\n"
            f"✅ To‘g‘ri javoblar: {correct_count} ta\n"
            f"❌ Noto‘g‘ri javoblar: {wrong_count} ta\n"
            f"📐 Qobiliyat (Theta): <code>{theta:+} logit</code>\n"
        )

        try:
            await callback.message.edit_text(result_text, parse_mode="HTML")
        except TelegramBadRequest:
            pass

        await state.clear()
        await callback.message.answer("Asosiy menyu:", reply_markup=main_menu)

        # Kanalga yuborish
        channel_post = (
            f"📢 <b>Yangi test natijasi qayd etildi!</b>\n\n"
            f"🔑 <b>Test ID:</b> <code>{test_code}</code>\n"
            f"👤 <b>O‘quvchi:</b> {safe_user_name}\n"
            f"🏆 <b>O‘rin:</b> <b>{rank_badge}</b>\n"
            f"🎯 <b>Rasch balli:</b> <b>{scaled_score} / 100</b> ({grade})\n"
            f"📊 <b>To‘g‘ri javoblar:</b> {correct_count} / {total_q} ta\n"
            f"📐 <b>Qobiliyat ko‘rsatkichi:</b> <code>{theta:+} logit</code>"
        )

        try:
            await bot.send_message(chat_id=CHANNEL_ID, text=channel_post, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Kanalga yuborishda xatolik: {e}")


# ==================== JAVOBLAR VA O'RINLARNI BILISH ====================
@dp.message(F.text == "📊 Javoblar va o‘rinlar")
async def start_check_answers(message: types.Message, state: FSMContext):
    await state.clear()
    await state.set_state(CheckAnswersAndRank.waiting_for_id)
    await message.answer(
        "Natijalari va javoblarini bilmoqchi bo‘lgan <b>Test ID</b> raqamini kiriting:\n\n"
        "<i>Bekor qilish uchun '❌ Bekor qilish' ni bosing.</i>",
        reply_markup=cancel_menu,
        parse_mode="HTML",
    )


@dp.message(CheckAnswersAndRank.waiting_for_id, F.text)
async def show_answers_and_ranks(message: types.Message, state: FSMContext):
    if message.text == "❌ Bekor qilish":
        await cancel_handler(message, state)
        return

    test_code = message.text.strip()

    async with aiosqlite.connect(DB_NAME) as db:
        # Test ma'lumotlarini olish
        async with db.execute("SELECT answers FROM tests WHERE code = ?", (test_code,)) as cursor:
            test_row = await cursor.fetchone()

        if not test_row:
            await message.answer("⚠️ Bunday ID li test topilmadi. Qaytadan tekshirib kiriting:")
            return

        answers = test_row[0]

        # O'quvchilar natijalarini saralab olish (har bir o'quvchining eng yaxshi natijasi)
        async with db.execute(
            """
            WITH Ranked AS (
                SELECT user_name, score, grade, correct_count, total_q,
                       ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY score DESC, id DESC) as rn
                FROM results
                WHERE test_code = ?
            )
            SELECT user_name, score, grade, correct_count, total_q
            FROM Ranked
            WHERE rn = 1
            ORDER BY score DESC
            LIMIT 20
            """,
            (test_code,),
        ) as cursor:
            ranking_rows = await cursor.fetchall()

    # To'g'ri javoblar kaliti matni
    formatted_answers = " ".join([f"<b>{i+1}.</b>{ans}" for i, ans in enumerate(answers)])

    msg = f"🔑 <b>TEST ID:</b> <code>{test_code}</code>\n\n"
    msg += f"📋 <b>To‘g‘ri javoblar kaliti:</b>\n{formatted_answers}\n"
    msg += "──────────────────────────\n"
    msg += "🏆 <b>O‘QUVCHILAR O‘RINLARI REYTINGI:</b>\n\n"

    if ranking_rows:
        for i, row in enumerate(ranking_rows, start=1):
            name, score, grade, correct, total = row
            safe_name = html.escape(name or "Noma'lum")
            if i == 1:
                badge = "🥇 1-o‘rin"
            elif i == 2:
                badge = "🥈 2-o‘rin"
            elif i == 3:
                badge = "🥉 3-o‘rin"
            else:
                badge = f"▫️ {i}-o‘rin"

            msg += f"{badge}: <b>{safe_name}</b> — <b>{score} ball</b> ({grade})\n"
            msg += f"    └ <i>To'g'ri: {correct}/{total} ta</i>\n"
    else:
        msg += "<i>Hozircha bu test bo‘yicha hech kim natija qayd etmagan.</i>\n"

    # Telegram xabar uzunligi (4096 belgi) limitini hisobga olish
    if len(msg) > 4000:
        chunks = [msg[i : i + 4000] for i in range(0, len(msg), 4000)]
        for chunk in chunks:
            await message.answer(chunk, parse_mode="HTML")
        await message.answer("Menyuga qaytildi:", reply_markup=main_menu)
    else:
        await message.answer(msg, reply_markup=main_menu, parse_mode="HTML")

    await state.clear()


# ==================== NOMA'LUM XABARLAR ====================
@dp.message()
async def default_handler(message: types.Message):
    await message.answer(
        "Iltimos, quyidagi menyudan foydalaning yoki qaytadan boshlash uchun /start buyrug‘ini bosing:",
        reply_markup=main_menu,
    )


# ==================== RENDER / HOSTING WEB SERVER ====================
async def handle_ping(request):
    return web.Response(text="Bot muvaffaqiyatli ishlamoqda!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Veb-server {port}-portda ishga tushdi...")


# ==================== ISHGA TUSHIRISH ====================
async def main():
    await init_db()
    await start_web_server()
    print("Rasch test boti muvaffaqiyatli ishga tushdi...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())