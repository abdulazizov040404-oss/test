import asyncio
import datetime
import html
import logging
import math
import os
import random
import re
import time
import aiosqlite
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
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

# Bosh admin (Super Admin)
SUPER_ADMIN_IDS = [7041478070]
SUPER_ADMIN_USERNAMES = ["durbek_coder"]

# O'zbekiston (Toshkent) vaqt mintaqasi (UTC+5)
TIMEZONE_UZ = datetime.timezone(datetime.timedelta(hours=5))

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())


def format_uz_time(ts: float) -> str:
    """Unix timestampni Toshkent vaqti bilan chiroyli formatda chiqarish."""
    dt = datetime.datetime.fromtimestamp(ts, tz=TIMEZONE_UZ)
    return dt.strftime("%d.%m.%Y, soat %H:%M")


# ==================== RASCH MODELI HISOBI ====================
def calculate_rasch(user_responses: list[int], difficulties: list[float]):
    total_items = len(user_responses)
    raw_score = sum(user_responses)

    if total_items == 0:
        return 0.0, 0.0, "Yiqildi"

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


# ==================== MA'LUMOTLAR BAZASI VA HUQUQLAR ====================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        # Testlar jadvali
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE,
                creator_id INTEGER,
                photo_id TEXT,
                answers TEXT,
                difficulties TEXT,
                content_type TEXT DEFAULT 'photo',
                content_text TEXT DEFAULT '',
                duration_minutes INTEGER DEFAULT 60,
                end_time REAL DEFAULT 0,
                is_finished INTEGER DEFAULT 0,
                created_at REAL DEFAULT 0
            )
        """)

        # tests jadvali migratsiyasi
        async with db.execute("PRAGMA table_info(tests)") as cursor:
            cols = [row[1] for row in await cursor.fetchall()]
            if "content_type" not in cols:
                await db.execute("ALTER TABLE tests ADD COLUMN content_type TEXT DEFAULT 'photo'")
            if "content_text" not in cols:
                await db.execute("ALTER TABLE tests ADD COLUMN content_text TEXT DEFAULT ''")
            if "duration_minutes" not in cols:
                await db.execute("ALTER TABLE tests ADD COLUMN duration_minutes INTEGER DEFAULT 60")
            if "end_time" not in cols:
                await db.execute("ALTER TABLE tests ADD COLUMN end_time REAL DEFAULT 0")
            if "is_finished" not in cols:
                await db.execute("ALTER TABLE tests ADD COLUMN is_finished INTEGER DEFAULT 0")
            if "created_at" not in cols:
                await db.execute("ALTER TABLE tests ADD COLUMN created_at REAL DEFAULT 0")

        # Natijalar jadvali
        await db.execute("""
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_code TEXT,
                user_id INTEGER,
                user_name TEXT,
                user_answers TEXT DEFAULT '',
                submitted_at REAL DEFAULT 0,
                score REAL DEFAULT 0,
                theta REAL DEFAULT 0,
                correct_count INTEGER DEFAULT 0,
                total_q INTEGER DEFAULT 0,
                grade TEXT DEFAULT '',
                is_notified INTEGER DEFAULT 0
            )
        """)

        # results jadvali migratsiyasi
        async with db.execute("PRAGMA table_info(results)") as cursor:
            cols = [row[1] for row in await cursor.fetchall()]
            if "user_answers" not in cols:
                await db.execute("ALTER TABLE results ADD COLUMN user_answers TEXT DEFAULT ''")
            if "submitted_at" not in cols:
                await db.execute("ALTER TABLE results ADD COLUMN submitted_at REAL DEFAULT 0")
            if "is_notified" not in cols:
                await db.execute("ALTER TABLE results ADD COLUMN is_notified INTEGER DEFAULT 0")

        # Adminlar jadvali
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE,
                name TEXT DEFAULT '',
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("CREATE INDEX IF NOT EXISTS idx_tests_code ON tests(code)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_results_test ON results(test_code, score)")
        await db.commit()


async def is_super_admin(user) -> bool:
    if isinstance(user, int):
        return user in SUPER_ADMIN_IDS
    if not user:
        return False
    if getattr(user, "id", None) in SUPER_ADMIN_IDS:
        return True
    username = getattr(user, "username", None)
    if username and username.lower().replace("@", "") in [u.lower() for u in SUPER_ADMIN_USERNAMES]:
        if user.id not in SUPER_ADMIN_IDS:
            SUPER_ADMIN_IDS.append(user.id)
        return True
    return False


async def is_admin(user) -> bool:
    if await is_super_admin(user):
        return True
    uid = user if isinstance(user, int) else getattr(user, "id", 0)
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT 1 FROM admins WHERE user_id = ?", (uid,)) as cursor:
            row = await cursor.fetchone()
            return bool(row)


# ==================== FSM HOLATLARI ====================
class CreateTest(StatesGroup):
    waiting_for_material = State()
    waiting_for_answers = State()
    waiting_for_diff = State()
    waiting_for_duration = State()


class SolveTest(StatesGroup):
    waiting_for_id = State()
    answering = State()


class CheckAnswersAndRank(StatesGroup):
    waiting_for_id = State()


class AdminPanelState(StatesGroup):
    waiting_for_add_id = State()
    waiting_for_del_id = State()


# ==================== KLAVIATURALAR ====================
async def get_main_menu(user) -> ReplyKeyboardMarkup:
    super_admin = await is_super_admin(user)
    admin_user = await is_admin(user)

    keyboard = [
        [KeyboardButton(text="📝 Test yechish")],
    ]

    if admin_user:
        keyboard.append([
            KeyboardButton(text="➕ Test qo'shish"),
            KeyboardButton(text="📊 Javoblar va o‘rinlar"),
        ])
    else:
        keyboard.append([
            KeyboardButton(text="📊 Javoblar va o‘rinlar"),
        ])

    if super_admin:
        keyboard.append([
            KeyboardButton(text="👑 Adminlar paneli"),
        ])

    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


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

duration_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="⏱ 15 daqiqa"), KeyboardButton(text="⏱ 30 daqiqa")],
        [KeyboardButton(text="⏱ 45 daqiqa"), KeyboardButton(text="⏱ 60 daqiqa (1 soat)")],
        [KeyboardButton(text="⏱ 120 daqiqa (2 soat)"), KeyboardButton(text="⏱ 1440 daqiqa (24 soat)")],
        [KeyboardButton(text="❌ Bekor qilish")],
    ],
    resize_keyboard=True,
)

admin_panel_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Admin tayinlash"), KeyboardButton(text="➖ Adminni o‘chirish")],
        [KeyboardButton(text="🔙 Asosiy menyu")],
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
    user_id = message.from_user.id
    menu = await get_main_menu(user_id)

    welcome_text = (
        f"👋 <b>Assalomu alaykum, {user_name}!</b>\n\n"
        f"🎯 <b>Rasch modeli</b> asosidagi intellektual test tizimiga xush kelibsiz!\n\n"
        f"Bu bot orqali siz testlarni topshirishingiz va o‘z bilimingizni xalqaro ilmiy <b>Rasch modeli</b> orqali xolis baholashingiz mumkin.\n\n"
        f"⚠️ <b>MUHIM QOIDALAR:</b>\n"
        f"──────────────────────────\n"
        f"1️⃣ <b>Vaqt chegarasi:</b> Har bir test uchun aniq vaqt belgilanadi.\n"
        f"2️⃣ <b>To‘g‘ri javoblar va ballar:</b> Firibgarlikning oldini olish maqsadida, to‘g‘ri javoblar kaliti va har bir o‘quvchining bali faqat <b>test vaqti to‘liq tugagandan so‘ng</b> e’lon qilinadi.\n"
        f"3️⃣ <b>Reyting:</b> Test vaqti tugagach, to‘g‘ri javoblar va barcha o‘quvchilar o‘rinlari birdaniga sizga hamda rasmiy kanalga yuboriladi.\n"
        f"──────────────────────────\n\n"
        f"🚀 <i>Boshlash uchun quyidagi bo‘limlardan birini tanlang:</i>"
    )

    await message.answer(welcome_text, reply_markup=menu, parse_mode="HTML")


@dp.message(F.text.in_({"❌ Bekor qilish", "🔙 Asosiy menyu"}))
async def cancel_handler(message: types.Message, state: FSMContext):
    await state.clear()
    menu = await get_main_menu(message.from_user.id)
    await message.answer("Asosiy menyuga qaytdingiz:", reply_markup=menu)


# ==================== SUPER ADMIN PANELI ====================
@dp.message(F.text == "👑 Adminlar paneli")
async def open_admin_panel(message: types.Message, state: FSMContext):
    await state.clear()
    if not await is_super_admin(message.from_user):
        await message.answer("⛔️ Bu bo‘lim faqat Super Admin uchun!", reply_markup=await get_main_menu(message.from_user))
        return

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id, name, added_at FROM admins ORDER BY id ASC") as cursor:
            admins = await cursor.fetchall()

    text = (
        "👑 <b>Adminlar boshqaruv paneli</b>\n\n"
        f"⭐ <b>Bosh Admin (Super Admin):</b> @durbek_coder (ID: <code>{message.from_user.id}</code>)\n\n"
        "📋 <b>Tayinlangan adminlar ro‘yxati:</b>\n"
    )

    if admins:
        for idx, (uid, uname, udate) in enumerate(admins, start=1):
            date_clean = str(udate)[:16] if udate else ""
            safe_u = html.escape(uname or "Ismsiz")
            text += f"{idx}. <code>{uid}</code> — <b>{safe_u}</b> (<i>{date_clean}</i>)\n"
    else:
        text += "<i>Hozircha tayinlangan qo‘shimcha adminlar yo‘q.</i>\n"

    text += "\n<i>Yangi admin qo‘shish yoki o‘chirish uchun quyidagi tugmalardan foydalaning:</i>"
    await message.answer(text, reply_markup=admin_panel_menu, parse_mode="HTML")


@dp.message(F.text == "➕ Admin tayinlash")
async def start_add_admin(message: types.Message, state: FSMContext):
    if not await is_super_admin(message.from_user):
        return
    await state.set_state(AdminPanelState.waiting_for_add_id)
    await message.answer(
        "Yangi admin bo‘ladigan foydalanuvchining <b>Telegram ID</b> raqamini kiriting:\n\n"
        "<i>(Foydalanuvchi o‘z ID raqamini @userinfobot orqali bilib olishi mumkin)</i>",
        reply_markup=cancel_menu,
        parse_mode="HTML",
    )


@dp.message(AdminPanelState.waiting_for_add_id, F.text)
async def process_add_admin(message: types.Message, state: FSMContext):
    if message.text in {"❌ Bekor qilish", "🔙 Asosiy menyu"}:
        await cancel_handler(message, state)
        return

    text = message.text.strip()
    if not text.isdigit():
        await message.answer("⚠️ Iltimos, faqat raqamlardan iborat Telegram ID kiriting:")
        return

    new_id = int(text)
    if new_id in SUPER_ADMIN_IDS:
        await message.answer("Ushbu foydalanuvchi allaqachon Super Admin hisoblanadi!")
        return

    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute(
                "INSERT INTO admins (user_id, name, added_by) VALUES (?, ?, ?)",
                (new_id, f"Admin {new_id}", message.from_user.id),
            )
            await db.commit()
            success = True
        except aiosqlite.IntegrityError:
            success = False

    await state.clear()
    if success:
        await message.answer(
            f"✅ <code>{new_id}</code> ID li foydalanuvchi muvaffaqiyatli <b>Admin</b> etib tayinlandi!\n"
            f"Endi u botda bemalol testlar qo‘sha oladi.",
            reply_markup=admin_panel_menu,
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"⚠️ <code>{new_id}</code> ID li foydalanuvchi allaqachon adminlar ro‘yxatida mavjud.",
            reply_markup=admin_panel_menu,
            parse_mode="HTML",
        )


@dp.message(F.text == "➖ Adminni o‘chirish")
async def start_del_admin(message: types.Message, state: FSMContext):
    if not await is_super_admin(message.from_user):
        return
    await state.set_state(AdminPanelState.waiting_for_del_id)
    await message.answer(
        "O‘chirmoqchi bo‘lgan adminning <b>Telegram ID</b> raqamini kiriting:",
        reply_markup=cancel_menu,
        parse_mode="HTML",
    )


@dp.message(AdminPanelState.waiting_for_del_id, F.text)
async def process_del_admin(message: types.Message, state: FSMContext):
    if message.text in {"❌ Bekor qilish", "🔙 Asosiy menyu"}:
        await cancel_handler(message, state)
        return

    text = message.text.strip()
    if not text.isdigit():
        await message.answer("⚠️ Iltimos, faqat raqamlardan iborat Telegram ID kiriting:")
        return

    del_id = int(text)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM admins WHERE user_id = ?", (del_id,))
        await db.commit()

    await state.clear()
    await message.answer(
        f"✅ <code>{del_id}</code> ID li admin ro‘yxatdan o‘chirildi.",
        reply_markup=admin_panel_menu,
        parse_mode="HTML",
    )


# Tezkor buyruqlar
@dp.message(Command("addadmin"))
async def cmd_addadmin(message: types.Message):
    if not await is_super_admin(message.from_user):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Foydalanish: <code>/addadmin &lt;TELEGRAM_ID&gt;</code>", parse_mode="HTML")
        return
    new_id = int(args[1])
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute(
                "INSERT INTO admins (user_id, name, added_by) VALUES (?, ?, ?)",
                (new_id, f"Admin {new_id}", message.from_user.id),
            )
            await db.commit()
            await message.answer(f"✅ Admin <code>{new_id}</code> muvaffaqiyatli qo‘shildi!", parse_mode="HTML")
        except Exception:
            await message.answer("⚠️ Ushbu foydalanuvchi allaqachon admin.")


@dp.message(Command("deladmin"))
async def cmd_deladmin(message: types.Message):
    if not await is_super_admin(message.from_user):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Foydalanish: <code>/deladmin &lt;TELEGRAM_ID&gt;</code>", parse_mode="HTML")
        return
    del_id = int(args[1])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM admins WHERE user_id = ?", (del_id,))
        await db.commit()
    await message.answer(f"✅ Admin <code>{del_id}</code> o‘chirildi.", parse_mode="HTML")


@dp.message(Command("stoptest"))
async def cmd_stoptest(message: types.Message):
    if not await is_admin(message.from_user):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Foydalanish: <code>/stoptest &lt;TEST_CODE&gt;</code>", parse_mode="HTML")
        return
    test_code = args[1].strip()
    await message.answer(f"⏳ Test <code>{test_code}</code> muddatidan oldin yakunlanmoqda...", parse_mode="HTML")
    await finalize_test(test_code)
    await message.answer(f"✅ Test <code>{test_code}</code> yakunlandi va barcha natijalar e’lon qilindi!", parse_mode="HTML")


# ==================== TEST QO'SHISH (FAQAT ADMINLAR) ====================
@dp.message(F.text == "➕ Test qo'shish")
async def start_add_test(message: types.Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user):
        menu = await get_main_menu(message.from_user)
        await message.answer(
            "⛔️ <b>Kechirasiz, yangi test qo‘shish huquqi faqat adminlarga berilgan!</b>\n\n"
            "Agar siz admin bo‘lsangiz, iltimos, Bosh adminga (@durbek_coder) murojaat qiling.",
            reply_markup=menu,
            parse_mode="HTML",
        )
        return

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
    if message.text in {"❌ Bekor qilish", "🔙 Asosiy menyu"}:
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
    if message.text in {"❌ Bekor qilish", "🔙 Asosiy menyu"}:
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
    if message.text in {"❌ Bekor qilish", "🔙 Asosiy menyu"}:
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
    await state.update_data(diff_str=diff_str)

    # 4-qadam: Vaqtni (davomiylikni) belgilash
    await state.set_state(CreateTest.waiting_for_duration)
    await message.answer(
        "⏱ <b>Test vaqtini (davomiyligini) belgilang:</b>\n\n"
        "Quyidagi tugmalardan birini tanlang yoki o‘zingiz daqiqa sonini yozib yuboring (masalan: <b>40</b>):\n\n"
        "⚠️ <b>Qoidaga ko‘ra:</b>\n"
        "• O‘quvchilar test yechganda to‘g‘ri javoblar va ballar darhol ko‘rsatilmaydi.\n"
        "• Belgilangan vaqt tugashi bilan to‘g‘ri javoblar kaliti va barcha o‘quvchilar natijalari/o‘rinlari birdaniga e’lon qilinadi!",
        reply_markup=duration_menu,
        parse_mode="HTML",
    )


@dp.message(CreateTest.waiting_for_duration, F.text)
async def get_test_duration(message: types.Message, state: FSMContext):
    if message.text in {"❌ Bekor qilish", "🔙 Asosiy menyu"}:
        await cancel_handler(message, state)
        return

    # Raqamni ajratib olish
    match = re.search(r"\d+", message.text)
    if not match:
        await message.answer("⚠️ Iltimos, daqiqa sonini raqamda kiriting (masalan: 30 yoki 45):")
        return

    duration_minutes = int(match.group(0))
    if duration_minutes < 1 or duration_minutes > 10080:
        await message.answer("⚠️ Test vaqti 1 daqiqadan 7 kungacha (10080 daqiqa) bo‘lishi mumkin!")
        return

    data = await state.get_data()
    n = len(data["answers"])
    diff_str = data["diff_str"]

    created_at = time.time()
    end_time = created_at + (duration_minutes * 60)

    # Unikal Test ID generatsiya qilish
    async with aiosqlite.connect(DB_NAME) as db:
        while True:
            test_code = str(random.randint(10000, 99999))
            async with db.execute("SELECT 1 FROM tests WHERE code = ?", (test_code,)) as cursor:
                if not await cursor.fetchone():
                    break

        await db.execute(
            """
            INSERT INTO tests (code, creator_id, photo_id, answers, difficulties, content_type, content_text, duration_minutes, end_time, is_finished, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                test_code,
                message.from_user.id,
                data.get("file_id", ""),
                data["answers"],
                diff_str,
                data.get("content_type", "photo"),
                data.get("content_text", ""),
                duration_minutes,
                end_time,
                created_at,
            ),
        )
        await db.commit()

    await state.clear()
    menu = await get_main_menu(message.from_user.id)
    end_time_str = format_uz_time(end_time)

    await message.answer(
        f"✅ <b>Test muvaffaqiyatli yaratildi va faollashtirildi!</b>\n\n"
        f"🔑 <b>Test ID:</b> <code>{test_code}</code>\n"
        f"📊 <b>Savollar soni:</b> {n} ta\n"
        f"⏱ <b>Ajratilgan vaqt:</b> {duration_minutes} daqiqa\n"
        f"⏳ <b>Yakunlanish vaqti:</b> <b>{end_time_str}</b> (Toshkent vaqti)\n\n"
        f"📢 <i>O‘quvchilarga ushbu Test ID raqamni bering.</i>\n\n"
        f"🔒 <b>Eslatma:</b> O‘quvchilar javob berganida to‘g‘ri javoblar ko‘rsatilmaydi. "
        f"Test vaqti tugashi bilan (<b>{end_time_str}</b>) barcha natijalar va to‘g‘ri javoblar kaliti barcha qatnashuvchilarga hamda kanalga avtomatik yuboriladi!",
        reply_markup=menu,
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
    if message.text in {"❌ Bekor qilish", "🔙 Asosiy menyu"}:
        await cancel_handler(message, state)
        return

    test_code = message.text.strip()
    user_id = message.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            """
            SELECT photo_id, answers, difficulties, content_type, content_text, duration_minutes, end_time, is_finished
            FROM tests WHERE code = ?
            """,
            (test_code,),
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            await message.answer("⚠️ Bunday ID li test topilmadi. Qaytadan tekshirib kiriting:")
            return

        # Oldin javob topshirganmi tekshirish
        async with db.execute(
            "SELECT 1 FROM results WHERE test_code = ? AND user_id = ?",
            (test_code, user_id),
        ) as cursor:
            already_done = await cursor.fetchone()

    photo_id, answers, diff_str, content_type, content_text, duration_min, end_time, is_finished = row
    now = time.time()

    # Test vaqti tugaganmi tekshirish
    if is_finished or (end_time > 0 and now >= end_time):
        await state.clear()
        menu = await get_main_menu(user_id)
        await message.answer(
            f"⚠️ <b>Ushbu test uchun qabul tugagan!</b>\n\n"
            f"Test vaqti yakunlangan. To‘g‘ri javoblar va umumiy o‘rinlar reytingini bilish uchun asosiy menyudan "
            f"<b>'📊 Javoblar va o‘rinlar'</b> bo‘limiga kiring.",
            reply_markup=menu,
            parse_mode="HTML",
        )
        return

    # Oldin topshirgan bo'lsa
    if already_done:
        await state.clear()
        menu = await get_main_menu(user_id)
        end_str = format_uz_time(end_time) if end_time > 0 else "Tez orada"
        await message.answer(
            f"⚠️ <b>Siz ushbu testga javoblaringizni topshirib bo‘lgansiz!</b>\n\n"
            f"Qoidaga ko‘ra har bir o‘quvchi testni faqat 1 marta topshira oladi.\n\n"
            f"⏳ Test vaqti tugagach (<b>{end_str}</b>), to‘g‘ri javoblar kaliti va sizning natijangiz ushbu bot orqali sizga avtomatik ravishda yuboriladi.",
            reply_markup=menu,
            parse_mode="HTML",
        )
        return

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
        end_time=end_time,
    )

    rem_seconds = max(0, int(end_time - now))
    rem_min = rem_seconds // 60

    caption_text = (
        f"📋 <b>Test ID:</b> <code>{test_code}</code>\n"
        f"📊 Jami savollar: {total_q} ta.\n"
        f"⏳ <b>Qolgan vaqt:</b> taxminan {rem_min} daqiqa.\n\n"
        f"Quyidagi variantlardan javoblarni belgilang:"
    )

    # 1. Avval test savollarini (rasm, PDF yoki yozma matn) to'liq chiqaramiz
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
        safe_content = html.escape(content_text)
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
    menu = await get_main_menu(callback.from_user.id)
    if not data or "current_q" not in data:
        await callback.message.edit_text("⚠️ Test muddati tugagan yoki bekor qilingan.")
        await callback.message.answer("Asosiy menyu:", reply_markup=menu)
        return

    action = callback.data.split("_")[1]

    # Testni to'xtatish tanlansa
    if action == "cancel":
        await state.clear()
        try:
            await callback.message.edit_text("❌ Test yechish to‘xtatildi.")
        except TelegramBadRequest:
            pass
        await callback.message.answer("Asosiy menyuga qaytdingiz:", reply_markup=menu)
        return

    end_time = data.get("end_time", 0)
    now = time.time()

    # Agar javob belgilayotganda vaqt tugab qolgan bo'lsa
    if end_time > 0 and now >= end_time:
        user_answers = list(data.get("user_answers", []))
        test_code = data["test_code"]
        total_q = data["total_q"]
        raw_user_name = callback.from_user.full_name or "Foydalanuvchi"

        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                """
                INSERT INTO results (test_code, user_id, user_name, user_answers, submitted_at, total_q, is_notified)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
                (test_code, callback.from_user.id, raw_user_name, "".join(user_answers), now, total_q),
            )
            await db.commit()

        await state.clear()
        try:
            await callback.message.edit_text(
                f"⏳ <b>Afsuski, test vaqti tugadi!</b>\n\n"
                f"Siz shu paytgacha belgilagan <b>{len(user_answers)} ta</b> savol bo‘yicha javoblaringiz qabul qilindi.\n\n"
                f"Test vaqti to‘liq yakunlangach, barcha natijalar e’lon qilinadi.",
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            pass
        await callback.message.answer("Asosiy menyuga qaytdingiz:", reply_markup=menu)
        return

    user_answers = list(data.get("user_answers", []))
    user_answers.append(action)

    current_q = data["current_q"] + 1
    total_q = data["total_q"]
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
        # Barcha savollar yechildi - JAVOBLARNI SAQLAYMIZ (lekin natijani hozir ko'rsatmaymiz!)
        raw_user_name = callback.from_user.full_name or "Foydalanuvchi"
        safe_user_name = html.escape(raw_user_name)
        answers_str = "".join(user_answers)
        end_str = format_uz_time(end_time) if end_time > 0 else "Vaqt tugaganda"

        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                """
                INSERT INTO results (test_code, user_id, user_name, user_answers, submitted_at, total_q, is_notified)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
                (test_code, callback.from_user.id, raw_user_name, answers_str, now, total_q),
            )
            await db.commit()

        success_text = (
            f"✅ <b>Javoblaringiz muvaffaqiyatli qabul qilindi!</b>\n\n"
            f"👤 <b>O‘quvchi:</b> {safe_user_name}\n"
            f"🔑 <b>Test ID:</b> <code>{test_code}</code>\n"
            f"📊 <b>Belgilangan savollar:</b> {len(user_answers)} / {total_q} ta\n"
            f"⏳ <b>Test yakunlanish vaqti:</b> <b>{end_str}</b>\n\n"
            f"🔒 <b>MUHIM:</b> Sinovning adolatli o‘tishi uchun, to‘g‘ri javoblar va to‘plagan balingiz hozir ko‘rsatilmaydi.\n\n"
            f"Test muddati (<b>{end_str}</b>) to‘liq yakunlangach:\n"
            f"• To‘g‘ri javoblar kaliti\n"
            f"• To‘plagan balingiz va egallagan o‘rningiz\n"
            f"avtomatik ravishda ushbu bot orqali sizga yuboriladi va rasmiy kanalga e’lon qilinadi!"
        )

        try:
            await callback.message.edit_text(success_text, parse_mode="HTML")
        except TelegramBadRequest:
            pass

        await state.clear()
        await callback.message.answer("Asosiy menyu:", reply_markup=menu)


# ==================== TESTNI YAKUNLASH VA NATIJALARNI BIRGALIKDA YUBORISH ====================
async def finalize_test(test_code: str):
    """Test muddati tugaganda barcha qatnashuvchilarga va kanalga natijalarni va to'g'ri javoblarni yuborish."""
    async with aiosqlite.connect(DB_NAME) as db:
        # Testni yakunlangan deb belgilaymiz
        await db.execute("UPDATE tests SET is_finished = 1 WHERE code = ?", (test_code,))
        await db.commit()

        # Test ma'lumotlarini olish
        async with db.execute(
            "SELECT answers, difficulties, creator_id FROM tests WHERE code = ?",
            (test_code,),
        ) as cursor:
            test_row = await cursor.fetchone()

        if not test_row:
            return

        answers_str, diff_str, creator_id = test_row
        correct_answers = list(answers_str)
        diffs = [float(x) for x in diff_str.split(",")]
        total_q = len(correct_answers)

        # Muddatidan oldin va vaqtida topshirgan barcha javoblarni olish
        async with db.execute(
            """
            SELECT id, user_id, user_name, user_answers, submitted_at
            FROM results
            WHERE test_code = ?
            """,
            (test_code,),
        ) as cursor:
            submissions = await cursor.fetchall()

    formatted_answers = " ".join([f"<b>{i+1}.</b>{ans}" for i, ans in enumerate(correct_answers)])

    if not submissions:
        # Hech kim topshirmagan bo'lsa
        empty_msg = (
            f"🏁 <b>TEST YAKUNLANDI (ID: <code>{test_code}</code>)</b>\n\n"
            f"📋 <b>To‘g‘ri javoblar kaliti:</b>\n{formatted_answers}\n"
            f"──────────────────────────\n"
            f"<i>Ushbu test bo‘yicha hech qanday natija qayd etilmadi.</i>"
        )
        try:
            if CHANNEL_ID:
                await bot.send_message(chat_id=CHANNEL_ID, text=empty_msg, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Kanalga yuborishda xatolik: {e}")
        return

    # Har bir o'quvchining Rasch balli va qobiliyatini hisoblash
    evaluated_list = []
    async with aiosqlite.connect(DB_NAME) as db:
        for sub_id, u_id, u_name, u_ans_str, sub_time in submissions:
            u_ans_list = list(u_ans_str or "")
            responses = [
                1 if i < len(u_ans_list) and u_ans_list[i] == correct_answers[i] else 0
                for i in range(total_q)
            ]
            correct_count = sum(responses)
            theta, scaled_score, grade = calculate_rasch(responses, diffs)

            await db.execute(
                """
                UPDATE results
                SET score = ?, theta = ?, correct_count = ?, total_q = ?, grade = ?
                WHERE id = ?
                """,
                (scaled_score, theta, correct_count, total_q, grade, sub_id),
            )

            evaluated_list.append({
                "id": sub_id,
                "user_id": u_id,
                "user_name": u_name,
                "score": scaled_score,
                "theta": theta,
                "correct_count": correct_count,
                "grade": grade,
                "submitted_at": sub_time or 0,
            })
        await db.commit()

    # O'rinlarni saralash (Ball bo'yicha kamayish, bir xil bo'lsa vaqt bo'yicha)
    evaluated_list.sort(key=lambda x: (-x["score"], x["submitted_at"]))
    total_participants = len(evaluated_list)

    # 1. Har bir o'quvchiga shaxsiy natijasini va to'g'ri javoblar kalitini yuboramiz
    async with aiosqlite.connect(DB_NAME) as db:
        for idx, item in enumerate(evaluated_list, start=1):
            if idx == 1:
                rank_badge = "🥇 1-o‘rin"
            elif idx == 2:
                rank_badge = "🥈 2-o‘rin"
            elif idx == 3:
                rank_badge = "🥉 3-o‘rin"
            else:
                rank_badge = f"▫️ {idx}-o‘rin"

            safe_name = html.escape(item["user_name"] or "Foydalanuvchi")
            wrong_count = total_q - item["correct_count"]

            student_notification = (
                f"🏁 <b>Test vaqti tugadi va natijalar e’lon qilindi!</b>\n\n"
                f"🔑 <b>Test ID:</b> <code>{test_code}</code>\n"
                f"👤 <b>O‘quvchi:</b> {safe_name}\n"
                f"🏆 <b>Egallagan o‘rningiz:</b> <b>{rank_badge}</b> (jami {total_participants} nafar o‘quvchi orasida)\n"
                f"🎯 <b>Rasch balli:</b> <b>{item['score']} / 100</b> ({item['grade']})\n"
                f"✅ To‘g‘ri javoblar: {item['correct_count']} ta\n"
                f"❌ Noto‘g‘ri javoblar: {wrong_count} ta\n"
                f"📐 Qobiliyat ko‘rsatkichi (Theta): <code>{item['theta']:+} logit</code>\n\n"
                f"📋 <b>To‘g‘ri javoblar kaliti:</b>\n{formatted_answers}\n"
            )

            try:
                await bot.send_message(chat_id=item["user_id"], text=student_notification, parse_mode="HTML")
                await db.execute("UPDATE results SET is_notified = 1 WHERE id = ?", (item["id"],))
            except Exception as e:
                logging.warning(f"O'quvchi ({item['user_id']}) ga yuborib bo'lmadi: {e}")

        await db.commit()

    # 2. Kanalga barcha o'rinlar reytingi va to'g'ri javoblarni yuboramiz
    channel_post = (
        f"🏁 <b>TEST YAKUNLANDI VA NATIJALAR E'LON QILINDI!</b>\n\n"
        f"🔑 <b>Test ID:</b> <code>{test_code}</code>\n"
        f"👥 <b>Jami qatnashuvchilar:</b> {total_participants} nafar\n\n"
        f"📋 <b>To‘g‘ri javoblar kaliti:</b>\n{formatted_answers}\n"
        f"──────────────────────────\n"
        f"🏆 <b>O‘QUVCHILAR O‘RINLARI REYTINGI:</b>\n\n"
    )

    for i, item in enumerate(evaluated_list[:50], start=1):
        if i == 1:
            badge = "🥇 1-o‘rin"
        elif i == 2:
            badge = "🥈 2-o‘rin"
        elif i == 3:
            badge = "🥉 3-o‘rin"
        else:
            badge = f"▫️ {i}-o‘rin"

        s_name = html.escape(item["user_name"] or "Foydalanuvchi")
        channel_post += (
            f"{badge}: <b>{s_name}</b> — <b>{item['score']} ball</b> ({item['grade']})\n"
            f"    └ <i>To'g'ri: {item['correct_count']}/{total_q} ta</i>\n"
        )

    try:
        if CHANNEL_ID:
            if len(channel_post) > 4000:
                chunks = [channel_post[i : i + 4000] for i in range(0, len(channel_post), 4000)]
                for chunk in chunks:
                    await bot.send_message(chat_id=CHANNEL_ID, text=chunk, parse_mode="HTML")
            else:
                await bot.send_message(chat_id=CHANNEL_ID, text=channel_post, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Kanalga yuborishda xatolik: {e}")


# ==================== MUDDATI TUGAGAN TESTLARNI AVTOMATIK TEKSHIRISH (LOOP) ====================
async def check_test_deadlines_loop():
    """Har 10 soniyada muddati tugagan faol testlarni tekshirib yakunlaydi."""
    while True:
        try:
            now = time.time()
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.execute(
                    "SELECT code FROM tests WHERE is_finished = 0 AND end_time > 0 AND end_time <= ?",
                    (now,),
                ) as cursor:
                    expired_tests = await cursor.fetchall()

            for (code,) in expired_tests:
                logging.info(f"Test {code} vaqti tugadi. Natijalar barchaga yuborilmoqda...")
                await finalize_test(code)

        except Exception as e:
            logging.error(f"Deadline tekshiruvida xatolik: {e}")

        await asyncio.sleep(10)


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
    if message.text in {"❌ Bekor qilish", "🔙 Asosiy menyu"}:
        await cancel_handler(message, state)
        return

    test_code = message.text.strip()
    now = time.time()
    menu = await get_main_menu(message.from_user.id)

    async with aiosqlite.connect(DB_NAME) as db:
        # Test ma'lumotlarini olish
        async with db.execute(
            "SELECT answers, end_time, is_finished FROM tests WHERE code = ?",
            (test_code,),
        ) as cursor:
            test_row = await cursor.fetchone()

        if not test_row:
            await message.answer("⚠️ Bunday ID li test topilmadi. Qaytadan tekshirib kiriting:")
            return

        answers, end_time, is_finished = test_row

        # Agar test hali davom etayotgan bo'lsa (vaqti tugamagan bo'lsa)
        if not is_finished and (end_time > 0 and now < end_time):
            rem_sec = int(end_time - now)
            rem_min = max(1, rem_sec // 60)
            end_time_str = format_uz_time(end_time)

            await state.clear()
            await message.answer(
                f"⏳ <b>Ushbu test hali davom etmoqda!</b>\n\n"
                f"🔑 <b>Test ID:</b> <code>{test_code}</code>\n"
                f"⏱ <b>Yakunlanish vaqti:</b> <b>{end_time_str}</b>\n"
                f"⏳ Qolgan vaqt: taxminan <b>{rem_min} daqiqa</b>\n\n"
                f"🔒 <i>To‘g‘ri javoblar kaliti va barcha o‘quvchilar reytingi test vaqti to‘liq tugagandan so‘ng e’lon qilinadi.</i>",
                reply_markup=menu,
                parse_mode="HTML",
            )
            return

        # Test yakunlangan bo'lsa - reytingni olamiz
        async with db.execute(
            """
            SELECT user_name, score, grade, correct_count, total_q
            FROM results
            WHERE test_code = ?
            ORDER BY score DESC, submitted_at ASC
            LIMIT 50
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
        msg += "<i>Ushbu test bo‘yicha natija qayd etilmagan.</i>\n"

    if len(msg) > 4000:
        chunks = [msg[i : i + 4000] for i in range(0, len(msg), 4000)]
        for chunk in chunks:
            await message.answer(chunk, parse_mode="HTML")
        await message.answer("Menyuga qaytildi:", reply_markup=menu)
    else:
        await message.answer(msg, reply_markup=menu, parse_mode="HTML")

    await state.clear()


# ==================== NOMA'LUM XABARLAR ====================
@dp.message()
async def default_handler(message: types.Message):
    menu = await get_main_menu(message.from_user.id)
    await message.answer(
        "Iltimos, quyidagi menyudan foydalaning yoki qaytadan boshlash uchun /start buyrug‘ini bosing:",
        reply_markup=menu,
    )


# ==================== RENDER / HOSTING WEB SERVER ====================
async def handle_ping(request):
    return web.Response(text="Bot muvaffaqiyatli ishlamoqda!")


async def start_web_server():
    try:
        app = web.Application()
        app.router.add_get("/", handle_ping)
        app.router.add_get("/ping", handle_ping)
        app.router.add_get("/healthz", handle_ping)
        port = int(os.environ.get("PORT", 8080))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        print(f"Veb-server 0.0.0.0:{port} da ishga tushdi...")
    except Exception as e:
        logging.warning(f"Veb-server ogohlantirish (lokal muhitda normal): {e}")


async def render_keep_alive_loop():
    """Render bepul tarifida veb-xizmat 15 daqiqada uxlab qolmasligi uchun har 10 daqiqada o'ziga ping yuboradi."""
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not render_url:
        return
    logging.info(f"Render keep-alive faollashtirildi: {render_url}")
    while True:
        await asyncio.sleep(600)  # Har 10 daqiqada
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(render_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    logging.info(f"Render self-ping: HTTP {resp.status}")
        except Exception as e:
            logging.warning(f"Render self-ping ogohlantirish: {e}")


# ==================== ISHGA TUSHIRISH ====================
async def main():
    await init_db()
    await start_web_server()
    # Orqa fonda test muddatlarini doimiy tekshiruvchi vazifa
    asyncio.create_task(check_test_deadlines_loop())
    # Render uxlab qolmasligi uchun avtomatik self-ping
    asyncio.create_task(render_keep_alive_loop())
    print("Rasch test boti yangilangan tizim bilan ishga tushdi...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())