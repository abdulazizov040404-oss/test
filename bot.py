import asyncio
import datetime
import html
import json
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
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# ==================== SOZLAMALAR ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.environ.get("BOT_TOKEN", "9055733774:AAHIr8SxoXOTmz_nECP6VAIS8TtuKMmQI7U")
CHANNEL_ID = "@etest14"
CHANNEL_URL = "https://t.me/etest14"
# Render'da persistent disk yo'lini DB_PATH orqali bering:
DB_NAME = os.environ.get("DB_PATH") or os.path.join(BASE_DIR, "tests_rasch.db")

SEED_FILES = [
    os.path.join(BASE_DIR, "seed_questions.json"),
    os.path.join(BASE_DIR, "seed_generated.json"),
    os.path.join(BASE_DIR, "seed_curriculum.json"),
]

SUBJECT_NAMES = {
    "english": "Ingliz tili",
    "math": "Matematika",
    "geometry": "Geometriya",
}
PRACTICE_NAMES = {**SUBJECT_NAMES, "mixed": "Aralash (Matematika + Geometriya)"}
LEVEL_NAMES = {"easy": "Oson", "medium": "O‘rtacha", "hard": "Qiyin"}
LEVEL_BADGES = {"easy": "🟢", "medium": "🟡", "hard": "🔴"}
PRACTICE_SIZES = (10, 20, 30, 45)
DEFAULT_PRACTICE_SIZE = 30

GEOMETRY_RE = re.compile(
    r"(perimetr|yuzini|yuzasi|\byuza|hajmini|\bhajm|aylana|doira|shar(?!q)|kub|konus|"
    r"silindr|parallelepiped|trapes|romb|parallelogramm|uchburchak|to‘rtburchak|"
    r"burchak|gradus|kesma|vatar|urinma|prizma|piramida|sfera|geometriya|pifagor|"
    r"gipotenuza|katet|diagonal|koordinata)",
    re.I,
)


def is_geometry_question(text: str) -> bool:
    """Matematika bazasidagi geometriyaga oid savolni aniqlaydi."""
    t = (text or "").lower()
    if "ehtimol" in t or "sharq" in t or "koeffitsiyent" in t:
        return False
    return bool(GEOMETRY_RE.search(t))

# Bosh admin (Super Admin)
SUPER_ADMIN_IDS = [7041478070]

# O'zbekiston (Toshkent) vaqt mintaqasi (UTC+5)
TIMEZONE_UZ = datetime.timezone(datetime.timedelta(hours=5))

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

FINALIZING_TESTS: set[str] = set()


def connect_db():
    return aiosqlite.connect(DB_NAME, timeout=15)


def chunk_text(text: str, limit: int = 4000) -> list[str]:
    """Matnni qator chegaralarida bo'laklarga bo'ladi (HTML teglar buzilmaydi)."""
    if len(text) <= limit:
        return [text]
    parts = []
    for line in text.split("\n"):
        while len(line) > limit:
            parts.append(line[:limit])
            line = line[limit:]
        parts.append(line)
    chunks, current = [], ""
    for part in parts:
        if not current:
            current = part
        elif len(current) + 1 + len(part) <= limit:
            current += "\n" + part
        else:
            chunks.append(current)
            current = part
    if current:
        chunks.append(current)
    return chunks or [text]


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
    async with connect_db() as db:
        try:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
        except Exception as e:
            logging.warning(f"WAL rejimini yoqishda ogohlantirish: {e}")

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
                results_posted INTEGER DEFAULT 0,
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
            if "results_posted" not in cols:
                await db.execute("ALTER TABLE tests ADD COLUMN results_posted INTEGER DEFAULT 0")

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
                is_notified INTEGER DEFAULT 0,
                is_submitted INTEGER DEFAULT 1
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
            if "is_submitted" not in cols:
                await db.execute("ALTER TABLE results ADD COLUMN is_submitted INTEGER DEFAULT 1")

        # Eski takroriy natijalarni tozalash (unikal indeks uchun)
        try:
            await db.execute(
                "DELETE FROM results WHERE id NOT IN (SELECT MAX(id) FROM results GROUP BY test_code, user_id)"
            )
        except Exception as e:
            logging.warning(f"Takroriy natijalarni tozalashda xatolik: {e}")

        # Bir foydalanuvchi bir testga faqat bitta natija yozishi uchun
        try:
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_results_unique ON results(test_code, user_id)"
            )
        except Exception as e:
            logging.warning(f"results unikal indeksi yaratilmadi: {e}")

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

        # Savollar bazasi
        await db.execute("""
            CREATE TABLE IF NOT EXISTS question_bank (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL,
                level TEXT NOT NULL,
                question TEXT NOT NULL,
                option_a TEXT NOT NULL,
                option_b TEXT NOT NULL,
                option_c TEXT NOT NULL,
                option_d TEXT NOT NULL,
                correct TEXT NOT NULL,
                photo_id TEXT DEFAULT '',
                source TEXT DEFAULT 'admin',
                created_by INTEGER DEFAULT 0,
                created_at REAL DEFAULT 0
            )
        """)

        # Sinov natijalari
        await db.execute("""
            CREATE TABLE IF NOT EXISTS practice_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                user_name TEXT,
                subject TEXT,
                level TEXT,
                correct_count INTEGER DEFAULT 0,
                total_q INTEGER DEFAULT 0,
                percent REAL DEFAULT 0,
                level_result TEXT DEFAULT '',
                created_at REAL DEFAULT 0
            )
        """)

        # Milliy CEFR hujjatlari
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cefr_docs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT DEFAULT '',
                file_id TEXT DEFAULT '',
                file_name TEXT DEFAULT '',
                subject TEXT DEFAULT 'math',
                added_at REAL DEFAULT 0
            )
        """)
        async with db.execute("PRAGMA table_info(cefr_docs)") as cursor:
            cefr_cols = [row[1] for row in await cursor.fetchall()]
        if "subject" not in cefr_cols:
            await db.execute("ALTER TABLE cefr_docs ADD COLUMN subject TEXT DEFAULT 'math'")

        await db.execute("CREATE INDEX IF NOT EXISTS idx_qb_subject_level ON question_bank(subject, level)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tests_code ON tests(code)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_results_test ON results(test_code, score)")
        await db.commit()


async def is_super_admin(user) -> bool:
    if isinstance(user, int):
        return user in SUPER_ADMIN_IDS
    return bool(user) and getattr(user, "id", None) in SUPER_ADMIN_IDS


async def is_admin(user) -> bool:
    if await is_super_admin(user):
        return True
    uid = user if isinstance(user, int) else getattr(user, "id", 0)
    async with connect_db() as db:
        async with db.execute("SELECT 1 FROM admins WHERE user_id = ?", (uid,)) as cursor:
            row = await cursor.fetchone()
            return bool(row)


def norm_question(text: str) -> str:
    return " ".join((text or "").split()).lower()


def escape_mojibake(text: str) -> str:
    """Eski seed fayllardan tushgan (cid:...) belgilarini tiklaydi."""
    return (text or "").replace("(cid:214)", "√")


async def repair_db():
    """Eski bazadagi to'ldirilmagan maydonlarni va manglangan savollarni tuzatadi."""
    now = time.time()
    async with connect_db() as db:
        # results: bo'sh qiymatlarni to'ldirish
        await db.execute("UPDATE results SET user_answers = '' WHERE user_answers IS NULL")
        await db.execute("UPDATE results SET total_q = 0 WHERE total_q IS NULL")
        await db.execute("UPDATE results SET submitted_at = 0 WHERE submitted_at IS NULL")
        await db.execute("UPDATE results SET is_submitted = 1 WHERE is_submitted IS NULL")

        # tests: vaqtlarni tiklash
        await db.execute(
            "UPDATE tests SET duration_minutes = 60 WHERE duration_minutes IS NULL OR duration_minutes <= 0"
        )
        await db.execute(
            "UPDATE tests SET created_at = end_time - duration_minutes * 60 "
            "WHERE (created_at IS NULL OR created_at = 0) AND end_time > 0"
        )
        # Vaqti noma'lum eski testlar: ularni yakunlangan deb belgilaymiz (kanalga spam bo'lmasin)
        await db.execute(
            "UPDATE tests SET created_at = ?, end_time = ?, is_finished = 1, results_posted = 1 "
            "WHERE (created_at IS NULL OR created_at = 0) AND (end_time IS NULL OR end_time = 0)",
            (now, now - 1),
        )
        await db.execute(
            "UPDATE tests SET end_time = created_at + duration_minutes * 60 "
            "WHERE (end_time IS NULL OR end_time = 0) AND created_at > 0"
        )
        await db.execute(
            "UPDATE tests SET content_type = 'photo' "
            "WHERE content_type IS NULL OR content_type NOT IN ('photo', 'document', 'text')"
        )
        await db.execute("UPDATE tests SET content_text = '' WHERE content_text IS NULL")
        await db.execute("UPDATE tests SET is_finished = 0 WHERE is_finished IS NULL")
        await db.execute("UPDATE tests SET results_posted = 0 WHERE results_posted IS NULL")

        # question_bank: belgilarni tiklash va bo'sh joylarni tozalash
        for col in ("question", "option_a", "option_b", "option_c", "option_d"):
            try:
                await db.execute(f"UPDATE question_bank SET {col} = REPLACE({col}, '(cid:214)', '√')")
            except Exception:
                pass
        try:
            await db.execute(
                "UPDATE question_bank SET source = 'legacy-fix' "
                "WHERE question LIKE '%(cid:%' AND source != 'admin'"
            )
            async with db.execute(
                "SELECT id, question, option_a, option_b, option_c, option_d FROM question_bank"
            ) as cursor:
                for qid, q, a, b, c, d in await cursor.fetchall():
                    clean = tuple(" ".join((v or "").split()) for v in (q, a, b, c, d))
                    if clean != (q, a, b, c, d):
                        await db.execute(
                            "UPDATE question_bank SET question = ?, option_a = ?, option_b = ?, "
                            "option_c = ?, option_d = ? WHERE id = ?",
                            (*clean, qid),
                        )
        except Exception as e:
            logging.warning(f"question_bank normalizatsiyasida xatolik: {e}")

        # Matematika bazasidagi geometriya savollarini alohida fanga ko'chirish
        try:
            async with db.execute(
                "SELECT id, question FROM question_bank WHERE subject = 'math'"
            ) as cursor:
                geom_ids = [
                    qid for qid, q in await cursor.fetchall()
                    if is_geometry_question(q)
                ]
            for qid in geom_ids:
                await db.execute(
                    "UPDATE question_bank SET subject = 'geometry' WHERE id = ?", (qid,)
                )
            if geom_ids:
                logging.info(f"Geometriyaga ko'chirildi: {len(geom_ids)} ta savol.")
        except Exception as e:
            logging.warning(f"geometry migratsiyasida xatolik: {e}")

        await db.commit()


async def seed_question_bank():
    """Seed fayllardan yangi savollarni qo'shadi va mavjudlaridagi xato javoblarni tuzatadi."""
    async with connect_db() as db:
        async with db.execute(
            "SELECT id, subject, level, question, option_a, option_b, option_c, option_d, correct, source "
            "FROM question_bank"
        ) as cursor:
            rows = await cursor.fetchall()
        existing = {}
        for r in rows:
            existing[(r[1], norm_question(r[3]))] = r

        now = time.time()
        added = 0
        fixed = 0
        for seed_file in SEED_FILES:
            if not os.path.exists(seed_file):
                logging.warning(f"Seed fayl topilmadi: {seed_file}")
                continue
            with open(seed_file, "r", encoding="utf-8") as f:
                items = json.load(f)
            for it in items:
                subject = it["subject"]
                if subject == "math" and is_geometry_question(it["question"]):
                    subject = "geometry"
                key = (subject, norm_question(it["question"]))
                row = existing.get(key)
                if row is None:
                    await db.execute(
                        """
                        INSERT INTO question_bank
                        (subject, level, question, option_a, option_b, option_c, option_d, correct, photo_id, source, created_by, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, 0, ?)
                        """,
                        (
                            subject, it["level"], it["question"],
                            it["option_a"], it["option_b"], it["option_c"], it["option_d"],
                            it["correct"], it.get("source", "seed"), now,
                        ),
                    )
                    added += 1
                    existing[key] = (None, subject, it["level"], it["question"],
                                     it["option_a"], it["option_b"], it["option_c"],
                                     it["option_d"], it["correct"], it.get("source", "seed"))
                    continue

                if row[9] == "admin":
                    continue

                new_vals = (
                    it["level"], it["question"],
                    it["option_a"], it["option_b"], it["option_c"], it["option_d"],
                    it["correct"],
                )
                if tuple(row[2:9]) != new_vals:
                    await db.execute(
                        "UPDATE question_bank SET level = ?, question = ?, option_a = ?, option_b = ?, "
                        "option_c = ?, option_d = ?, correct = ? WHERE id = ?",
                        (*new_vals, row[0]),
                    )
                    fixed += 1
        await db.commit()
        if added or fixed:
            logging.info(f"Savollar bazasi: {added} ta qo'shildi, {fixed} ta tuzatildi.")


# ==================== MAJBURIY OBUNA ====================
def get_subscription_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Kanalga obuna bo‘lish", url=CHANNEL_URL)],
            [InlineKeyboardButton(text="✅ Tekshirish", callback_data="sub_check")],
        ]
    )


async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        status = getattr(member, "status", "")
        value = getattr(status, "value", status)
        return str(value) in {"member", "administrator", "creator"}
    except Exception as e:
        # Bot kanalda admin bo'lmasa Telegram xato qaytaradi — botni bloklamaymiz
        logging.warning(f"Obuna tekshiruvida xatolik: {e}")
        return True


def get_gate_text() -> str:
    return (
        "🔒 <b>Botdan foydalanish uchun avval rasmiy kanalga obuna bo‘ling.</b>\n\n"
        f"📢 {CHANNEL_URL}\n\n"
        "<i>Obuna bo‘lgach, «✅ Tekshirish» tugmasini bosing.</i>"
    )


async def ensure_subscribed(message: types.Message) -> bool:
    if await is_subscribed(message.from_user.id):
        return True
    await message.answer(get_gate_text(), reply_markup=get_subscription_kb(), parse_mode="HTML")
    return False


async def ensure_subscribed_cb(callback: types.CallbackQuery) -> bool:
    if await is_subscribed(callback.from_user.id):
        return True
    await callback.answer("Avval rasmiy kanalga obuna bo‘ling!", show_alert=True)
    await callback.message.answer(
        get_gate_text(), reply_markup=get_subscription_kb(), parse_mode="HTML"
    )
    return False


@dp.callback_query(F.data == "sub_check")
async def sub_check(callback: types.CallbackQuery):
    if await is_subscribed(callback.from_user.id):
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass
        await callback.answer("✅ Obuna tasdiqlandi!")
        await callback.message.answer(
            "Asosiy menyu:", reply_markup=await get_main_menu(callback.from_user)
        )
    else:
        await callback.answer(
            "❌ Obuna hali tasdiqlanmadi. Kanalga obuna bo‘ling.", show_alert=True
        )


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


class PracticeTest(StatesGroup):
    answering = State()


class AddQuestion(StatesGroup):
    waiting_question = State()
    waiting_options = State()


class UploadQuestions(StatesGroup):
    waiting_file = State()


class EditQuestion(StatesGroup):
    waiting_id = State()
    waiting_value = State()


class DeleteQuestions(StatesGroup):
    waiting_ids = State()


class CefrAdd(StatesGroup):
    waiting_doc = State()
    waiting_title = State()


# ==================== KLAVIATURALAR ====================
async def get_main_menu(user) -> ReplyKeyboardMarkup:
    super_admin = await is_super_admin(user)
    admin_user = await is_admin(user)

    keyboard = [
        [KeyboardButton(text="📝 Test yechish")],
        [
            KeyboardButton(text="📘 Ingliz tili"),
            KeyboardButton(text="➗ Matematika"),
        ],
        [
            KeyboardButton(text="📐 Geometriya"),
            KeyboardButton(text="🔀 Aralash"),
        ],
        [KeyboardButton(text="🇺🇿 Milliy CEFR")],
        [KeyboardButton(text="📊 Javoblar va o‘rinlar")],
    ]

    if admin_user:
        keyboard.append([
            KeyboardButton(text="➕ Test qo'shish"),
            KeyboardButton(text="🗂 Savollar bazasi"),
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

bank_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Savol qo'shish"), KeyboardButton(text="📥 Fayldan yuklash")],
        [KeyboardButton(text="✏️ Savolni tahrirlash"), KeyboardButton(text="🗑 Savolni o'chirish")],
        [KeyboardButton(text="📤 Eksport"), KeyboardButton(text="📊 Statistika")],
        [KeyboardButton(text="🔙 Asosiy menyu")],
    ],
    resize_keyboard=True,
)


def get_bank_subject_kb(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📘 Ingliz tili", callback_data=f"{prefix}:english"),
                InlineKeyboardButton(text="➗ Matematika", callback_data=f"{prefix}:math"),
            ],
            [InlineKeyboardButton(text="📐 Geometriya", callback_data=f"{prefix}:geometry")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="qb:cancel")],
        ]
    )


def get_bank_level_kb(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🟢 Oson", callback_data=f"{prefix}:easy"),
                InlineKeyboardButton(text="🟡 O‘rtacha", callback_data=f"{prefix}:medium"),
            ],
            [InlineKeyboardButton(text="🔴 Qiyin", callback_data=f"{prefix}:hard")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="qb:cancel")],
        ]
    )


def get_correct_kb(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="A", callback_data=f"{prefix}:A"),
                InlineKeyboardButton(text="B", callback_data=f"{prefix}:B"),
                InlineKeyboardButton(text="C", callback_data=f"{prefix}:C"),
                InlineKeyboardButton(text="D", callback_data=f"{prefix}:D"),
            ]
        ]
    )


def get_options_keyboard(current_q: int = 1, total_q: int = 0):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="A", callback_data="ans_A"),
                InlineKeyboardButton(text="B", callback_data="ans_B"),
                InlineKeyboardButton(text="C", callback_data="ans_C"),
                InlineKeyboardButton(text="D", callback_data="ans_D"),
            ],
            [
                InlineKeyboardButton(text="🏁 Testni tugatish", callback_data="ans_finish"),
            ],
            [
                InlineKeyboardButton(
                    text="⏸ Chiqish (javoblar saqlanadi)", callback_data="ans_exit"
                ),
            ],
        ]
    )


# ==================== /START VA BEKOR QILISH ====================
@dp.message(CommandStart())
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    if not await ensure_subscribed(message):
        return
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

    async with connect_db() as db:
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

    async with connect_db() as db:
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
    async with connect_db() as db:
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
    async with connect_db() as db:
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
    async with connect_db() as db:
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
    async with connect_db() as db:
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
def get_test_list_kb(tests) -> InlineKeyboardMarkup:
    rows = []
    now = time.time()
    for code, answers, end_time in tests:
        rem_min = max(1, int(end_time - now) // 60) if end_time else 0
        rows.append([
            InlineKeyboardButton(
                text=f"🟢 {code} · {len(answers)} savol · {rem_min} daq",
                callback_data=f"tsel:{code}",
            )
        ])
    rows.append([InlineKeyboardButton(text="✍️ ID raqam bilan kirish", callback_data="tsel:manual")])
    rows.append([InlineKeyboardButton(text="❌ Bekor qilish", callback_data="tsel:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def save_test_result(test_code, user_id, user_name, answers_str, total_q, submitted=False):
    now = time.time()
    async with connect_db() as db:
        await db.execute(
            """
            INSERT INTO results (test_code, user_id, user_name, user_answers, submitted_at, total_q, is_notified, is_submitted)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(test_code, user_id) DO UPDATE SET
                user_name = excluded.user_name,
                user_answers = excluded.user_answers,
                submitted_at = CASE WHEN excluded.submitted_at > 0 THEN excluded.submitted_at ELSE results.submitted_at END,
                is_submitted = MAX(results.is_submitted, excluded.is_submitted)
            """,
            (
                test_code, user_id, user_name, answers_str,
                now if submitted else 0, total_q, 1 if submitted else 0,
            ),
        )
        await db.commit()


def build_submit_text(test_code, user_name, answered, total_q, end_time):
    end_str = format_uz_time(end_time) if end_time else "tez orada"
    safe_name = html.escape(user_name or "Foydalanuvchi")
    return (
        f"✅ <b>Javoblaringiz qabul qilindi!</b>\n\n"
        f"👤 <b>O‘quvchi:</b> {safe_name}\n"
        f"🔑 <b>Test ID:</b> <code>{test_code}</code>\n"
        f"✍️ <b>Belgilangan savollar:</b> <b>{answered} / {total_q} ta</b>\n"
        f"⏳ <b>Test yakunlanish vaqti:</b> <b>{end_str}</b>\n\n"
        f"🔒 <i>To‘g‘ri javoblar va natijalar test vaqti tugagach e’lon qilinadi.</i>"
    )


async def send_test_content(message, content_type, file_id, content_text, caption_text):
    caption = caption_text
    if content_type == "photo" and content_text and content_text.strip():
        merged = f"{html.escape(content_text.strip())}\n\n{caption_text}"
        if len(merged) <= 1000:
            caption = merged
    if content_type == "photo" and file_id:
        await message.answer_photo(
            photo=file_id, caption=caption,
            reply_markup=ReplyKeyboardRemove(), parse_mode="HTML",
        )
        return
    if content_type == "document" and file_id:
        await message.answer_document(
            document=file_id, caption=caption_text,
            reply_markup=ReplyKeyboardRemove(), parse_mode="HTML",
        )
        return
    if content_text:
        chunks = chunk_text(content_text, limit=3500)
        if len(chunks) == 1:
            await message.answer(
                f"📝 <b>Test savollari:</b>\n\n{html.escape(chunks[0])}\n\n{caption_text}",
                reply_markup=ReplyKeyboardRemove(),
                parse_mode="HTML",
            )
            return
        for i, chunk in enumerate(chunks):
            await message.answer(
                chunk, reply_markup=ReplyKeyboardRemove() if i == 0 else None
            )
        await message.answer(caption_text, parse_mode="HTML")
        return
    await message.answer(caption_text, reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")


async def begin_test(message: types.Message, state: FSMContext, test_code: str, user):
    user_id = user.id
    user_name = user.full_name or "Foydalanuvchi"
    now = time.time()

    async with connect_db() as db:
        async with db.execute(
            "SELECT photo_id, answers, difficulties, content_type, content_text, "
            "duration_minutes, end_time, is_finished FROM tests WHERE code = ?",
            (test_code,),
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            await message.answer("⚠️ Bunday ID li test topilmadi. Qaytadan tekshirib kiriting:")
            return

        async with db.execute(
            "SELECT user_answers, is_submitted FROM results WHERE test_code = ? AND user_id = ?",
            (test_code, user_id),
        ) as cursor:
            my_row = await cursor.fetchone()

    photo_id, answers_str, diff_str, content_type, content_text, duration_min, end_time, is_finished = row
    content_type = content_type or "photo"
    content_text = content_text or ""
    end_time = end_time or 0
    total_q = len(answers_str or "")
    try:
        diffs = [float(x) for x in (diff_str or "").split(",") if x]
    except ValueError:
        diffs = []
    if len(diffs) != total_q:
        diffs = [-1.5 + 3.0 * i / max(1, total_q - 1) for i in range(total_q)]

    if not total_q or not diffs:
        await message.answer("⚠️ Ushbu test ma’lumotlari to‘liq emas.")
        return

    if is_finished or (end_time > 0 and now >= end_time):
        await state.clear()
        await message.answer(
            "⚠️ <b>Ushbu test uchun qabul tugagan!</b>\n\n"
            "To‘g‘ri javoblar va umumiy o‘rinlar reytingini bilish uchun asosiy menyudan "
            "<b>'📊 Javoblar va o‘rinlar'</b> bo‘limiga kiring.",
            reply_markup=await get_main_menu(user_id),
            parse_mode="HTML",
        )
        return

    if my_row and my_row[1]:
        await state.clear()
        end_str = format_uz_time(end_time) if end_time > 0 else "tez orada"
        await message.answer(
            f"⚠️ <b>Siz ushbu testga javoblaringizni topshirib bo‘lgansiz!</b>\n\n"
            f"Har bir o‘quvchi testni faqat 1 marta topshira oladi.\n\n"
            f"⏳ Natijalar <b>{end_str}</b> da e’lon qilinadi.",
            reply_markup=await get_main_menu(user_id),
            parse_mode="HTML",
        )
        return

    saved = (my_row[0] if my_row else "") or ""
    answers = list(saved[:total_q].ljust(total_q, "-"))
    next_index = next(
        (i for i, a in enumerate(answers) if a not in {"A", "B", "C", "D"}), None
    )
    answered_count = sum(1 for a in answers if a in "ABCD")

    await state.set_state(SolveTest.answering)
    await state.update_data(
        test_code=test_code,
        correct_answers=answers_str,
        difficulties=diffs,
        answers=answers,
        next_index=next_index if next_index is not None else total_q,
        total_q=total_q,
        end_time=end_time,
    )

    rem_min = max(1, int(end_time - now) // 60) if end_time else 0
    caption_text = (
        f"📋 <b>Test ID:</b> <code>{test_code}</code>\n"
        f"📊 Jami savollar: {total_q} ta.\n"
        + (f"✍️ Belgilangan: <b>{answered_count}/{total_q}</b>\n" if answered_count else "")
        + f"⏳ <b>Qolgan vaqt:</b> taxminan {rem_min} daqiqa.\n\n"
        f"Quyidagi variantlardan javoblarni belgilang:"
    )

    await send_test_content(message, content_type, photo_id, content_text, caption_text)

    if next_index is None:
        await save_test_result(test_code, user_id, user_name, "".join(answers), total_q, submitted=True)
        await state.clear()
        await message.answer(
            build_submit_text(test_code, user_name, answered_count, total_q, end_time),
            parse_mode="HTML",
        )
        await message.answer("Asosiy menyu:", reply_markup=await get_main_menu(user_id))
        return

    header = f"<b>{next_index + 1}-savol</b> (jami {total_q} ta)"
    if answered_count:
        header += f"\n✍️ Belgilangan: <b>{answered_count}/{total_q}</b>"
    await message.answer(header, reply_markup=get_options_keyboard(), parse_mode="HTML")


@dp.message(F.text == "📝 Test yechish")
async def start_solve_test(message: types.Message, state: FSMContext):
    await state.clear()
    if not await ensure_subscribed(message):
        return

    now = time.time()
    async with connect_db() as db:
        async with db.execute(
            "SELECT code, answers, end_time FROM tests "
            "WHERE is_finished = 0 AND end_time > ? ORDER BY created_at DESC LIMIT 20",
            (now,),
        ) as cursor:
            tests = await cursor.fetchall()

    await state.set_state(SolveTest.waiting_for_id)
    if tests:
        lines = "\n".join(
            f"• <code>{code}</code> — {len(ans)} savol, {max(1, int(et - now) // 60)} daqiqa qoldi"
            for code, ans, et in tests
        )
        await message.answer(
            f"📝 <b>Faol testlar:</b>\n{lines}\n\n"
            "Quyidagidan tanlang yoki test ID raqamini yozib yuboring.\n\n"
            "<i>Bekor qilish uchun '❌ Bekor qilish'.</i>",
            reply_markup=get_test_list_kb(tests),
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "Hozircha faol testlar yo‘q.\n\nTest ID raqamini kiriting:\n\n"
            "<i>Bekor qilish uchun '❌ Bekor qilish' ni bosing.</i>",
            reply_markup=cancel_menu,
            parse_mode="HTML",
        )


@dp.callback_query(F.data.startswith("tsel:"))
async def solve_test_select(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    arg = callback.data.split(":", 1)[1]

    if arg == "cancel":
        await state.clear()
        try:
            await callback.message.edit_text(
                "❌ Test yechish bekor qilindi.", reply_markup=None
            )
        except TelegramBadRequest:
            pass
        await callback.message.answer(
            "Asosiy menyu:", reply_markup=await get_main_menu(callback.from_user)
        )
        return

    if arg == "manual":
        await state.set_state(SolveTest.waiting_for_id)
        try:
            await callback.message.edit_text(
                "Test ID raqamini yozib yuboring:\n\n<i>Bekor qilish uchun '❌ Bekor qilish'.</i>",
                parse_mode="HTML",
                reply_markup=None,
            )
        except TelegramBadRequest:
            pass
        await callback.message.answer("✍️ Test ID:", reply_markup=cancel_menu)
        return

    if not await ensure_subscribed_cb(callback):
        return

    await state.clear()
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    await begin_test(callback.message, state, arg, callback.from_user)


@dp.message(SolveTest.waiting_for_id, F.text)
async def enter_test_id(message: types.Message, state: FSMContext):
    if message.text in {"❌ Bekor qilish", "🔙 Asosiy menyu"}:
        await cancel_handler(message, state)
        return

    test_code = message.text.strip()
    if not re.fullmatch(r"\d{4,10}", test_code):
        await message.answer(
            "⚠️ Test ID faqat raqamlardan iborat. Masalan: <b>12345</b>", parse_mode="HTML"
        )
        return

    await begin_test(message, state, test_code, message.from_user)


@dp.message(SolveTest.answering)
async def test_answering_hint(message: types.Message, state: FSMContext):
    await message.answer(
        "ℹ️ Javobni <b>A</b>, <b>B</b>, <b>C</b> yoki <b>D</b> tugmalari orqali belgilang.\n"
        "Testni yakunlash uchun <b>🏁 Testni tugatish</b> tugmasini bosing.\n"
        "Javoblaringiz har safar avtomatik saqlanadi.",
        parse_mode="HTML",
    )


@dp.callback_query(SolveTest.answering, F.data.startswith("ans_"))
async def handle_answer_choice(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    menu = await get_main_menu(callback.from_user.id)
    if not data or "answers" not in data:
        await callback.answer("Test sessiyasi topilmadi", show_alert=True)
        await state.clear()
        try:
            await callback.message.edit_text(
                "⚠️ Test sessiyasi topilmadi. Testni qaytadan boshlang."
            )
        except TelegramBadRequest:
            pass
        await callback.message.answer("Asosiy menyu:", reply_markup=menu)
        return

    action = callback.data.split("_", 1)[1]
    test_code = data["test_code"]
    total_q = data["total_q"]
    end_time = data.get("end_time", 0) or 0
    now = time.time()
    user_name = callback.from_user.full_name or "Foydalanuvchi"
    answers = list(data.get("answers", []))
    if len(answers) < total_q:
        answers += ["-"] * (total_q - len(answers))

    # Test bazada hamon faolmi?
    async with connect_db() as db:
        async with db.execute(
            "SELECT is_finished FROM tests WHERE code = ?", (test_code,)
        ) as cursor:
            row = await cursor.fetchone()

    if not row or row[0] or (end_time > 0 and now >= end_time):
        await callback.answer("⚠️ Test vaqti tugagan!", show_alert=True)
        await state.clear()
        try:
            await callback.message.edit_text(
                "⏳ <b>Test vaqti tugadi.</b>\n\n"
                "Siz shu paytgacha belgilagan javoblaringiz saqlandi. "
                "Natijalar test to‘liq yakunlangach e’lon qilinadi.",
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            pass
        await callback.message.answer("Asosiy menyu:", reply_markup=menu)
        return

    # Tugatish tugmasi — javoblar saqlanadi va nechta belgilangani ko'rsatiladi
    if action == "finish":
        await callback.answer()
        answered = sum(1 for a in answers if a in "ABCD")
        await save_test_result(
            test_code, callback.from_user.id, user_name, "".join(answers), total_q, submitted=True
        )
        await state.clear()
        try:
            await callback.message.edit_text(
                build_submit_text(test_code, user_name, answered, total_q, end_time),
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            pass
        await callback.message.answer("Asosiy menyu:", reply_markup=menu)
        return

    # Chiqish — javoblar saqlanadi, keyin shu ID bilan davom ettirish mumkin
    if action == "exit":
        await callback.answer()
        await save_test_result(
            test_code, callback.from_user.id, user_name, "".join(answers), total_q, submitted=False
        )
        await state.clear()
        answered = sum(1 for a in answers if a in "ABCD")
        try:
            await callback.message.edit_text(
                f"⏸ <b>Testdan chiqdingiz.</b>\n\n"
                f"✍️ Belgilangan javoblar: <b>{answered}/{total_q}</b> ta saqlandi.\n\n"
                f"Davom ettirish uchun <b>📝 Test yechish</b> bo‘limidan "
                f"<code>{test_code}</code> testini qayta tanlang.",
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            pass
        await callback.message.answer("Asosiy menyu:", reply_markup=menu)
        return

    if action not in {"A", "B", "C", "D"}:
        await callback.answer()
        return

    await callback.answer()
    idx = data.get("next_index", 0)
    if idx >= total_q:
        idx = next((i for i, a in enumerate(answers) if a not in "ABCD"), total_q - 1)
    answers[idx] = action

    nxt = next((i for i in range(idx + 1, total_q) if answers[i] not in "ABCD"), None)
    answered = sum(1 for a in answers if a in "ABCD")

    if nxt is None:
        await save_test_result(
            test_code, callback.from_user.id, user_name, "".join(answers), total_q, submitted=True
        )
        await state.clear()
        try:
            await callback.message.edit_text(
                build_submit_text(test_code, user_name, answered, total_q, end_time),
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            pass
        await callback.message.answer("Asosiy menyu:", reply_markup=menu)
        return

    await save_test_result(
        test_code, callback.from_user.id, user_name, "".join(answers), total_q, submitted=False
    )
    await state.update_data(answers=answers, next_index=nxt)

    try:
        await callback.message.edit_text(
            f"<b>{nxt + 1}-savol</b> (jami {total_q} ta)\n"
            f"✍️ Belgilangan: <b>{answered}/{total_q}</b>",
            reply_markup=get_options_keyboard(),
            parse_mode="HTML",
        )
    except TelegramBadRequest:
        pass


# ==================== TESTNI YAKUNLASH VA NATIJALARNI BIRGALIKDA YUBORISH ====================
async def finalize_test(test_code: str):
    """Test muddati tugaganda barcha qatnashuvchilarga va kanalga natijalarni yuborish."""
    if test_code in FINALIZING_TESTS:
        return
    FINALIZING_TESTS.add(test_code)
    try:
        await _finalize_test(test_code)
    except Exception:
        logging.exception(f"Test {test_code} ni yakunlashda xatolik")
    finally:
        FINALIZING_TESTS.discard(test_code)


async def _finalize_test(test_code: str):
    async with connect_db() as db:
        async with db.execute(
            "SELECT answers, difficulties FROM tests WHERE code = ?", (test_code,)
        ) as cursor:
            test_row = await cursor.fetchone()

        if not test_row:
            return

        answers_str, diff_str = test_row
        correct_answers = list(answers_str or "")
        total_q = len(correct_answers)

        try:
            diffs = [float(x) for x in (diff_str or "").split(",") if x]
        except ValueError:
            diffs = []
        if len(diffs) != total_q:
            diffs = [-1.5 + 3.0 * i / max(1, total_q - 1) for i in range(total_q)]

        if not total_q:
            await db.execute(
                "UPDATE tests SET is_finished = 1, results_posted = 1 WHERE code = ?",
                (test_code,),
            )
            await db.commit()
            return

        # Testni yakunlangan deb belgilaymiz
        await db.execute(
            "UPDATE tests SET is_finished = 1 WHERE code = ? AND is_finished = 0",
            (test_code,),
        )
        await db.commit()

        # Barcha qatnashuvchilar (tugatmaganlar ham) — javoblari saqlangan
        async with db.execute(
            "SELECT id, user_id, user_name, user_answers, submitted_at, is_notified "
            "FROM results WHERE test_code = ? ORDER BY id",
            (test_code,),
        ) as cursor:
            submissions = await cursor.fetchall()

        async with db.execute(
            "SELECT results_posted FROM tests WHERE code = ?", (test_code,)
        ) as cursor:
            posted_row = await cursor.fetchone()

    formatted_answers = " ".join([f"<b>{i+1}.</b>{ans}" for i, ans in enumerate(correct_answers)])
    channel_done = bool(posted_row and posted_row[0])

    if not submissions:
        async with connect_db() as db:
            await db.execute("UPDATE tests SET results_posted = 1 WHERE code = ?", (test_code,))
            await db.commit()
        return

    # Har bir o'quvchining Rasch balli va qobiliyatini hisoblash
    evaluated_list = []
    async with connect_db() as db:
        for sub_id, u_id, u_name, u_ans_str, sub_time, notified in submissions:
            u_ans_str = u_ans_str or ""
            u_ans_list = list(u_ans_str)
            responses = [
                1 if i < len(u_ans_list) and u_ans_list[i] == correct_answers[i] else 0
                for i in range(total_q)
            ]
            correct_count = sum(responses)
            answered = sum(1 for ch in u_ans_str if ch in "ABCD")
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
                "answered": answered,
                "notified": notified,
                "grade": grade,
                "submitted_at": sub_time or 0,
            })
        await db.commit()

    # O'rinlarni saralash (Ball bo'yicha kamayish, bir xil bo'lsa vaqt bo'yicha)
    evaluated_list.sort(key=lambda x: (-x["score"], x["submitted_at"] or float("inf")))
    total_participants = len(evaluated_list)

    # 1. Har bir o'quvchiga shaxsiy natijasini va to'g'ri javoblar kalitini yuboramiz
    async with connect_db() as db:
        for idx, item in enumerate(evaluated_list, start=1):
            if item["notified"]:
                continue

            if idx == 1:
                rank_badge = "🥇 1-o‘rin"
            elif idx == 2:
                rank_badge = "🥈 2-o‘rin"
            elif idx == 3:
                rank_badge = "🥉 3-o‘rin"
            else:
                rank_badge = f"▫️ {idx}-o‘rin"

            safe_name = html.escape(item["user_name"] or "Foydalanuvchi")
            wrong_count = item["answered"] - item["correct_count"]
            unanswered = total_q - item["answered"]

            student_notification = (
                f"🏁 <b>Test vaqti tugadi va natijalar e’lon qilindi!</b>\n\n"
                f"🔑 <b>Test ID:</b> <code>{test_code}</code>\n"
                f"👤 <b>O‘quvchi:</b> {safe_name}\n"
                f"🏆 <b>Egallagan o‘rningiz:</b> <b>{rank_badge}</b> (jami {total_participants} nafar o‘quvchi orasida)\n"
                f"🎯 <b>Rasch balli:</b> <b>{item['score']} / 100</b> ({item['grade']})\n"
                f"✍️ Belgilangan savollar: {item['answered']} / {total_q} ta\n"
                f"✅ To‘g‘ri javoblar: {item['correct_count']} ta\n"
                f"❌ Noto‘g‘ri javoblar: {wrong_count} ta\n"
                f"➖ Javobsiz qolgan: {unanswered} ta\n"
                f"📐 Qobiliyat ko‘rsatkichi (Theta): <code>{item['theta']:+} logit</code>\n\n"
                f"📋 <b>To‘g‘ri javoblar kaliti:</b>\n{formatted_answers}\n"
            )

            try:
                for chunk in chunk_text(student_notification):
                    await bot.send_message(chat_id=item["user_id"], text=chunk, parse_mode="HTML")
                await db.execute("UPDATE results SET is_notified = 1 WHERE id = ?", (item["id"],))
            except Exception as e:
                logging.warning(f"O'quvchi ({item['user_id']}) ga yuborib bo'lmadi: {e}")

        await db.commit()

    # 2. Kanalga faqat to'g'ri javoblar kaliti va natijalar reytingini yuboramiz
    if channel_done or not CHANNEL_ID:
        return

    channel_post = (
        f"🏁 <b>TEST YAKUNLANDI — ID: <code>{test_code}</code></b>\n\n"
        f"📋 <b>To‘g‘ri javoblar kaliti:</b>\n{formatted_answers}\n"
        f"──────────────────────────\n"
        f"🏆 <b>NATIJALAR:</b>\n\n"
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
        for chunk in chunk_text(channel_post):
            await bot.send_message(chat_id=CHANNEL_ID, text=chunk, parse_mode="HTML")
        async with connect_db() as db:
            await db.execute("UPDATE tests SET results_posted = 1 WHERE code = ?", (test_code,))
            await db.commit()
    except Exception as e:
        logging.error(f"Kanalga yuborishda xatolik: {e}")


# ==================== MUDDATI TUGAGAN TESTLARNI AVTOMATIK TEKSHIRISH (LOOP) ====================
async def check_test_deadlines_loop():
    """Har 10 soniyada muddati tugagan faol testlarni tekshirib yakunlaydi."""
    while True:
        try:
            now = time.time()
            async with connect_db() as db:
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
    if not await ensure_subscribed(message):
        return
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

    async with connect_db() as db:
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
        for chunk in chunk_text(msg):
            await message.answer(chunk, parse_mode="HTML")
        await message.answer("Menyuga qaytildi:", reply_markup=menu)
    else:
        await message.answer(msg, reply_markup=menu, parse_mode="HTML")

    await state.clear()


# ==================== BILIM SINOVI (INGLIZ / MATEMATIKA / GEOMETRIYA / ARALASH) ====================
PRACTICE_BLOCK_ORDER = ("easy", "medium", "hard")
PRACTICE_SUBJECTS = {"english", "math", "geometry", "mixed"}

PRACTICE_BUTTONS = {
    "📘 Ingliz tili": "english",
    "➗ Matematika": "math",
    "📐 Geometriya": "geometry",
    "🔀 Aralash": "mixed",
}


def split_by_level(total: int) -> dict:
    base = total // len(PRACTICE_BLOCK_ORDER)
    counts = {lv: base for lv in PRACTICE_BLOCK_ORDER}
    counts["hard"] += total - base * len(PRACTICE_BLOCK_ORDER)
    return counts


async def _fetch_level_questions(db, subject: str, level: str, limit: int):
    if limit <= 0:
        return []
    async with db.execute(
        """
        SELECT id, question, option_a, option_b, option_c, option_d, correct, photo_id
        FROM question_bank
        WHERE subject = ? AND level = ? AND source != 'legacy-fix'
        ORDER BY RANDOM()
        LIMIT ?
        """,
        (subject, level, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        {
            "id": r[0], "question": r[1], "a": r[2], "b": r[3],
            "c": r[4], "d": r[5], "correct": r[6], "photo_id": r[7] or "",
            "level": level,
        }
        for r in rows
    ]


async def fetch_practice_questions(subject: str, size: int):
    """Har bir darajadan savollar olib, oson → o'rtacha → qiyin tartibida qaytaradi."""
    counts = split_by_level(size)
    questions = []
    async with connect_db() as db:
        for level in PRACTICE_BLOCK_ORDER:
            if subject == "mixed":
                need = counts[level]
                geo_need = need // 2
                level_qs = await _fetch_level_questions(db, "math", level, need - geo_need)
                level_qs += await _fetch_level_questions(db, "geometry", level, geo_need)
                random.shuffle(level_qs)
            else:
                level_qs = await _fetch_level_questions(db, subject, level, counts[level])
            questions.extend(level_qs)
    return questions


async def begin_practice(
    message: types.Message, state: FSMContext, subject: str, size: int = DEFAULT_PRACTICE_SIZE
):
    questions = await fetch_practice_questions(subject, size)
    if not questions:
        await message.answer(
            f"⚠️ <b>{PRACTICE_NAMES[subject]}</b> bo‘yicha savollar topilmadi.",
            parse_mode="HTML",
        )
        return

    await state.set_state(PracticeTest.answering)
    await state.update_data(
        subject=subject, size=size, questions=questions, index=0,
        answers=[], photo_msg_id=0,
    )

    counts = {lv: sum(1 for q in questions if q["level"] == lv) for lv in PRACTICE_BLOCK_ORDER}
    lines = "\n".join(
        f"{LEVEL_BADGES[lv]} {LEVEL_NAMES[lv]}: {counts[lv]} ta"
        for lv in PRACTICE_BLOCK_ORDER if counts[lv]
    )
    note = ""
    if len(questions) < size:
        note = f"\n\n<i>Bazada yetarli savol yo‘q — {len(questions)} ta olindi.</i>"
    await message.answer(
        f"🧠 <b>{PRACTICE_NAMES[subject]}</b> — bilim sinovi\n\n"
        f"Tartib: <b>osondan qiyinga</b> (aralash emas).\n{lines}\n\n"
        f"Jami <b>{len(questions)} ta</b> savol. Omad!{note}",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML",
    )
    await send_practice_question(message, state)


def get_practice_count_kb(subject: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text=f"{n} ta", callback_data=f"pcnt:{subject}:{n}")
            for n in PRACTICE_SIZES
        ],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"pcnt:{subject}:0")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def start_practice_subject(message: types.Message, state: FSMContext, subject: str):
    await state.clear()
    if not await ensure_subscribed(message):
        return
    await message.answer(
        f"🧠 <b>{PRACTICE_NAMES[subject]}</b>\n\nNechta savol bo‘lsin?",
        reply_markup=get_practice_count_kb(subject),
        parse_mode="HTML",
    )


@dp.message(F.text.in_(PRACTICE_BUTTONS))
async def practice_subject_button(message: types.Message, state: FSMContext):
    await start_practice_subject(message, state, PRACTICE_BUTTONS[message.text])


@dp.callback_query(F.data.startswith("pcnt:"))
async def practice_size_chosen(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    _, subject, raw = parts
    await callback.answer()
    if subject not in PRACTICE_SUBJECTS:
        return

    if raw == "0":
        await state.clear()
        try:
            await callback.message.edit_text("❌ Bilim sinovi bekor qilindi.")
        except TelegramBadRequest:
            pass
        await callback.message.answer(
            "Asosiy menyu:", reply_markup=await get_main_menu(callback.from_user)
        )
        return

    if not raw.isdigit() or int(raw) not in PRACTICE_SIZES:
        return
    if not await ensure_subscribed_cb(callback):
        return

    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    await begin_practice(callback.message, state, subject, int(raw))


@dp.callback_query(F.data.startswith("lv:"))
async def choose_practice_level(callback: types.CallbackQuery, state: FSMContext):
    # Eski xabarlardagi daraja tugmalari ham to'liq (oson→qiyin) sinovni boshlaydi
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return

    _, subject, level = parts

    if level == "cancel":
        await callback.answer()
        await state.clear()
        try:
            await callback.message.edit_text("❌ Bilim sinovi bekor qilindi.")
        except TelegramBadRequest:
            pass
        await callback.message.answer(
            "Asosiy menyu:", reply_markup=await get_main_menu(callback.from_user)
        )
        return

    await callback.answer()
    if subject not in PRACTICE_SUBJECTS:
        return
    if not await ensure_subscribed_cb(callback):
        return

    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    await begin_practice(callback.message, state, subject, DEFAULT_PRACTICE_SIZE)


async def send_practice_question(message: types.Message, state: FSMContext):
    data = await state.get_data()
    questions = data["questions"]
    index = data["index"]
    q = questions[index]
    total = len(questions)
    level = q.get("level", "easy")

    header = (
        f"{LEVEL_BADGES.get(level, '')} <b>{index + 1}-savol</b> (jami {total} ta)\n"
        f"<i>{LEVEL_NAMES.get(level, '')}</i>"
    )
    body = (
        f"{html.escape(q['question'])}\n\n"
        f"<b>A)</b> {html.escape(q['a'])}\n"
        f"<b>B)</b> {html.escape(q['b'])}\n"
        f"<b>C)</b> {html.escape(q['c'])}\n"
        f"<b>D)</b> {html.escape(q['d'])}"
    )
    if len(body) > 3900:
        body = body[:3890] + "…"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="A", callback_data="pans_A"),
                InlineKeyboardButton(text="B", callback_data="pans_B"),
                InlineKeyboardButton(text="C", callback_data="pans_C"),
                InlineKeyboardButton(text="D", callback_data="pans_D"),
            ],
            [InlineKeyboardButton(text="❌ To‘xtatish", callback_data="pans_cancel")],
        ]
    )

    if q["photo_id"]:
        photo_msg = await message.answer_photo(photo=q["photo_id"], parse_mode="HTML")
        await state.update_data(photo_msg_id=photo_msg.message_id)
    else:
        await state.update_data(photo_msg_id=0)
    await message.answer(f"{header}\n\n{body}", reply_markup=kb, parse_mode="HTML")


async def delete_practice_photo(callback: types.CallbackQuery, data: dict):
    photo_msg_id = data.get("photo_msg_id") or 0
    if not photo_msg_id:
        return
    try:
        await callback.bot.delete_message(callback.message.chat.id, photo_msg_id)
    except TelegramBadRequest:
        pass


@dp.callback_query(PracticeTest.answering, F.data.startswith("pans_"))
async def practice_answer(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    if not data or "questions" not in data:
        await state.clear()
        try:
            await callback.message.edit_text(
                "⚠️ Test sessiyasi topilmadi. Qaytadan boshlang."
            )
        except TelegramBadRequest:
            pass
        return

    action = callback.data.split("_")[1]

    if action == "cancel":
        await delete_practice_photo(callback, data)
        await state.clear()
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass
        await callback.message.answer(
            "❌ Bilim sinovi to‘xtatildi.",
            reply_markup=await get_main_menu(callback.from_user),
        )
        return

    if action not in {"A", "B", "C", "D"}:
        return

    questions = data["questions"]
    index = data["index"]
    answers = list(data.get("answers", []))
    answers.append(action)

    await delete_practice_photo(callback, data)
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass

    next_index = index + 1
    if next_index < len(questions):
        await state.update_data(index=next_index, answers=answers)
        await send_practice_question(callback.message, state)
    else:
        await finish_practice(callback, state, answers)


async def finish_practice(
    callback: types.CallbackQuery, state: FSMContext, answers: list[str]
):
    data = await state.get_data()
    questions = data["questions"]
    subject = data["subject"]
    total = len(questions)

    correct_count = sum(
        1 for q, a in zip(questions, answers) if a == q["correct"]
    )
    percent = round(correct_count / total * 100, 1) if total else 0.0

    if percent >= 80:
        result_level = "🟢 Yuqori"
    elif percent >= 50:
        result_level = "🟡 O‘rta"
    else:
        result_level = "🔴 Past"

    per_level = {lv: [0, 0] for lv in PRACTICE_BLOCK_ORDER}
    for q, a in zip(questions, answers):
        lv = q.get("level", "easy")
        per_level.setdefault(lv, [0, 0])
        per_level[lv][1] += 1
        if a == q["correct"]:
            per_level[lv][0] += 1

    async with connect_db() as db:
        await db.execute(
            """
            INSERT INTO practice_results
            (user_id, user_name, subject, level, correct_count, total_q, percent, level_result, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                callback.from_user.id,
                callback.from_user.full_name or "Foydalanuvchi",
                subject,
                "mixed",
                correct_count,
                total,
                percent,
                result_level,
                time.time(),
            ),
        )
        await db.commit()

    level_lines = "\n".join(
        f"{LEVEL_BADGES[lv]} {LEVEL_NAMES[lv]}: <b>{c}/{t}</b>"
        for lv, (c, t) in per_level.items() if t
    )
    key_line = "  ".join(f"{i + 1}-{q['correct']}" for i, q in enumerate(questions))
    summary = (
        f"🏁 <b>Bilim sinovi yakunlandi!</b>\n\n"
        f"📚 <b>Fan:</b> {PRACTICE_NAMES[subject]}\n"
        f"📊 <b>Darajalar bo‘yicha:</b>\n{level_lines}\n\n"
        f"✅ <b>To‘g‘ri javoblar:</b> {correct_count} / {total} ta\n"
        f"🎯 <b>Natija:</b> {percent}%\n"
        f"🏅 <b>Baholash:</b> {result_level}\n\n"
        f"📋 <b>Javoblar kaliti:</b>\n{key_line}"
    )

    await state.clear()
    for chunk in chunk_text(summary):
        await callback.message.answer(chunk, parse_mode="HTML")
    await callback.message.answer(
        "Asosiy menyu:", reply_markup=await get_main_menu(callback.from_user)
    )


# ==================== SAVOLLAR BAZASI (ADMIN) ====================
def parse_options_text(text: str):
    lines = []
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if len(re.findall(r"(?:^|\s)[A-D][).:]\s", line)) >= 2:
            lines.extend(p.strip() for p in re.split(r"(?=(?:^|\s)[A-D][).:]\s)", line) if p.strip())
        else:
            lines.append(line)

    parts = {}
    for line in lines:
        m = re.match(r"^([A-D])[).:]\s*(.+)$", line.strip(), re.I)
        if m:
            parts[m.group(1).upper()] = m.group(2).strip()

    if set(parts) != {"A", "B", "C", "D"} or not all(parts.values()):
        return None
    return parts


def parse_questions_txt(text: str):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", text)
    items, errors = [], []

    for bi, block in enumerate(blocks, start=1):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue

        qid = None
        m = re.match(r"^#ID\s*(\d+)$", lines[0], re.I)
        if m:
            qid = int(m.group(1))
            lines = lines[1:]

        question_lines, opts, correct = [], {}, None
        for line in lines:
            mj = re.match(
                r"^(?:Javob|Answer|To['‘’]g['‘’]ri javob)\s*[:：]\s*([A-D])\s*$",
                line,
                re.I,
            )
            mo = re.match(r"^([A-D])[).:]\s*(.+)$", line, re.I)
            if mj and correct is None:
                correct = mj.group(1).upper()
            elif mo and correct is None:
                if len(re.findall(r"(?:^|\s)[A-D][).:]\s", line)) >= 2:
                    for part in re.split(r"(?=(?:^|\s)[A-D][).:]\s)", line):
                        pm = re.match(r"^\s*([A-D])[).:]\s*(.+)$", part, re.I)
                        if pm:
                            opts[pm.group(1).upper()] = pm.group(2).strip()
                else:
                    opts[mo.group(1).upper()] = mo.group(2).strip()
            elif correct is None and not opts:
                question_lines.append(line)

        question = " ".join(question_lines).strip()
        if (
            not question
            or set(opts) != {"A", "B", "C", "D"}
            or not all(opts.values())
            or not correct
        ):
            errors.append(f"{bi}-blok: noto‘g‘ri format")
            continue

        items.append(
            {"id": qid, "question": question, "options": opts, "correct": correct}
        )

    return items, errors


async def insert_question(subject, level, question, opts, correct, photo_id, user_id):
    async with connect_db() as db:
        await db.execute(
            """
            INSERT INTO question_bank
            (subject, level, question, option_a, option_b, option_c, option_d, correct, photo_id, source, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'admin', ?, ?)
            """,
            (
                subject, level, question,
                opts["A"], opts["B"], opts["C"], opts["D"],
                correct, photo_id or "", user_id, time.time(),
            ),
        )
        await db.commit()


@dp.message(F.text == "🗂 Savollar bazasi")
async def open_question_bank(message: types.Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user):
        await message.answer(
            "⛔️ Bu bo‘lim faqat adminlar uchun!",
            reply_markup=await get_main_menu(message.from_user),
        )
        return
    await message.answer(
        "🗂 <b>Savollar bazasi</b>\n\nKerakli bo‘limni tanlang:",
        reply_markup=bank_menu,
        parse_mode="HTML",
    )


@dp.message(F.text == "📊 Statistika")
async def qb_stats(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user):
        return
    async with connect_db() as db:
        async with db.execute(
            "SELECT subject, level, COUNT(*) FROM question_bank GROUP BY subject, level"
        ) as cursor:
            rows = await cursor.fetchall()

    counts = {(s, l): c for s, l, c in rows}
    text = "📊 <b>Savollar bazasi statistikasi</b>\n\n"
    for subj in ("english", "math", "geometry"):
        text += f"<b>{SUBJECT_NAMES[subj]}</b>\n"
        for lvl in ("easy", "medium", "hard"):
            text += f"  • {LEVEL_NAMES[lvl]}: {counts.get((subj, lvl), 0)} ta\n"
        text += "\n"
    text += f"<b>Jami:</b> {sum(counts.values())} ta savol"

    await message.answer(text, reply_markup=bank_menu, parse_mode="HTML")


@dp.message(F.text == "➕ Savol qo'shish")
async def qb_add_start(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user):
        return
    await state.clear()
    await message.answer(
        "Qaysi fanga savol qo‘shamiz?", reply_markup=get_bank_subject_kb("qa")
    )


@dp.callback_query(F.data == "qb:cancel")
async def qb_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text("❌ Bekor qilindi.")
    except TelegramBadRequest:
        pass
    if await is_admin(callback.from_user):
        await callback.message.answer("Savollar bazasi:", reply_markup=bank_menu)
    await callback.answer()


@dp.callback_query(F.data.startswith("qa:"))
async def qb_add_subject(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user):
        await callback.answer("⛔️", show_alert=True)
        return
    subject = callback.data.split(":")[1]
    if subject not in SUBJECT_NAMES:
        await callback.answer()
        return
    await callback.message.edit_text(
        f"{SUBJECT_NAMES[subject]} — darajani tanlang:",
        reply_markup=get_bank_level_kb(f"qal:{subject}"),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("qal:"))
async def qb_add_level(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user):
        await callback.answer("⛔️", show_alert=True)
        return
    _, subject, level = callback.data.split(":")
    if subject not in SUBJECT_NAMES or level not in LEVEL_NAMES:
        await callback.answer()
        return

    await state.set_state(AddQuestion.waiting_question)
    await state.update_data(subject=subject, level=level)
    await callback.message.edit_text(
        f"✅ {SUBJECT_NAMES[subject]} / {LEVEL_NAMES[level]}\n\n"
        "Savol matnini yuboring (rasmli savol bo‘lsa — rasm va izoh yuboring):"
    )
    await callback.answer()


@dp.message(AddQuestion.waiting_question)
async def qb_add_question(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user):
        return

    if message.photo:
        question = (message.caption or "").strip()
        photo_id = message.photo[-1].file_id
    elif message.text:
        question = message.text.strip()
        photo_id = ""
    else:
        await message.answer("Iltimos, savol matnini yoki rasmni yuboring:")
        return

    if not question:
        await message.answer("Savol matni bo‘sh bo‘lmasligi kerak. Qaytadan yuboring:")
        return

    await state.update_data(question=question, photo_id=photo_id, options=None, correct=None)
    await state.set_state(AddQuestion.waiting_options)
    await message.answer(
        "Variantlarni bitta xabarda yuboring (har biri yangi qatorda yoki bitta qatorda):\n\n"
        "A) birinchi variant\nB) ikkinchi variant\nC) uchinchi variant\nD) to‘rtinchi variant",
        reply_markup=cancel_menu,
    )


@dp.message(AddQuestion.waiting_options, F.text)
async def qb_add_options(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user):
        return

    opts = parse_options_text(message.text)
    if not opts:
        await message.answer(
            "⚠️ 4 ta variantni A), B), C), D) shaklida yuboring. Qaytadan:"
        )
        return

    await state.update_data(options=opts)
    await message.answer(
        "To‘g‘ri javobni tanlang:", reply_markup=get_correct_kb("qc")
    )


@dp.message(AddQuestion.waiting_options)
async def qb_add_options_hint(message: types.Message):
    await message.answer("Iltimos, variantlarni matn ko‘rinishida yuboring:")


@dp.callback_query(F.data.startswith("qc:"))
async def qb_add_correct(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user):
        await callback.answer("⛔️", show_alert=True)
        return

    correct = callback.data.split(":")[1]
    data = await state.get_data()
    if not data.get("question") or not data.get("options"):
        await callback.answer("Sessiya eskirgan, qaytadan boshlang.", show_alert=True)
        return

    await insert_question(
        data["subject"], data["level"], data["question"],
        data["options"], correct, data.get("photo_id", ""),
        callback.from_user.id,
    )

    await callback.message.edit_text(
        f"✅ Savol saqlandi.\n\nTo‘g‘ri javob: <b>{correct}</b>\n\nYana savol qo‘shamizmi?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="➕ Ha", callback_data="qm:yes"),
                    InlineKeyboardButton(text="✅ Yo‘q", callback_data="qm:no"),
                ]
            ]
        ),
        parse_mode="HTML",
    )
    await callback.answer("✅ Saqlandi")


@dp.callback_query(F.data.startswith("qm:"))
async def qb_add_more(callback: types.CallbackQuery, state: FSMContext):
    if callback.data == "qm:yes":
        await state.set_state(AddQuestion.waiting_question)
        await callback.message.edit_text(
            "Keyingi savol matnini yuboring (yoki rasm + izoh):"
        )
    else:
        await state.clear()
        await callback.message.edit_text("✅ Savollar saqlandi.")
        await callback.message.answer("Savollar bazasi:", reply_markup=bank_menu)
    await callback.answer()


@dp.message(F.text == "📥 Fayldan yuklash")
async def qb_upload_start(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user):
        return
    await state.clear()
    await message.answer(
        "Qaysi fanga yuklaymiz?", reply_markup=get_bank_subject_kb("qu")
    )


@dp.callback_query(F.data.startswith("qu:"))
async def qb_upload_subject(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user):
        await callback.answer("⛔️", show_alert=True)
        return
    subject = callback.data.split(":")[1]
    if subject not in SUBJECT_NAMES:
        await callback.answer()
        return
    await callback.message.edit_text(
        f"{SUBJECT_NAMES[subject]} — darajani tanlang:",
        reply_markup=get_bank_level_kb(f"qul:{subject}"),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("qul:"))
async def qb_upload_level(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user):
        await callback.answer("⛔️", show_alert=True)
        return
    _, subject, level = callback.data.split(":")
    if subject not in SUBJECT_NAMES or level not in LEVEL_NAMES:
        await callback.answer()
        return

    await state.set_state(UploadQuestions.waiting_file)
    await state.update_data(subject=subject, level=level)
    await callback.message.edit_text(
        f"✅ {SUBJECT_NAMES[subject]} / {LEVEL_NAMES[level]}\n\n"
        "Endi <b>.txt</b> fayl yuboring. Format:\n\n"
        "<code>#ID 123\n"
        "Savol matni\n"
        "A) birinchi variant\n"
        "B) ikkinchi variant\n"
        "C) uchinchi variant\n"
        "D) to‘rtinchi variant\n"
        "Javob: B</code>\n\n"
        "<i>#ID qatori ixtiyoriy — mavjud savolni yangilaydi, bo‘lmasa yangi savol qo‘shiladi.\n"
        "Bloklar bo‘sh qator bilan ajratiladi.</i>",
        reply_markup=cancel_menu,
        parse_mode="HTML",
    )
    await callback.answer()


@dp.message(UploadQuestions.waiting_file, F.document)
async def qb_upload_file(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user):
        return

    file_name = message.document.file_name or ""
    if not file_name.lower().endswith((".txt", ".csv", ".md")):
        await message.answer("⚠️ Faqat .txt fayl yuboring.")
        return

    file = await bot.get_file(message.document.file_id)
    buffer = await bot.download_file(file.file_path)
    raw = buffer.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("cp1251", errors="replace")

    data = await state.get_data()
    subject = data.get("subject")
    level = data.get("level")
    if subject not in SUBJECT_NAMES or level not in LEVEL_NAMES:
        await state.clear()
        await message.answer(
            "⚠️ Sessiya eskirgan, qaytadan boshlang.", reply_markup=bank_menu
        )
        return

    items, errors = parse_questions_txt(content)
    inserted = updated = 0

    async with connect_db() as db:
        for it in items:
            if it["id"]:
                async with db.execute(
                    "SELECT 1 FROM question_bank WHERE id = ?", (it["id"],)
                ) as cursor:
                    exists = await cursor.fetchone()
                if not exists:
                    errors.append(f"#{it['id']}: savol topilmadi")
                    continue
                await db.execute(
                    """
                    UPDATE question_bank
                    SET subject = ?, level = ?, question = ?, option_a = ?, option_b = ?,
                        option_c = ?, option_d = ?, correct = ?
                    WHERE id = ?
                    """,
                    (
                        subject, level, it["question"],
                        it["options"]["A"], it["options"]["B"],
                        it["options"]["C"], it["options"]["D"],
                        it["correct"], it["id"],
                    ),
                )
                updated += 1
            else:
                await db.execute(
                    """
                    INSERT INTO question_bank
                    (subject, level, question, option_a, option_b, option_c, option_d, correct, photo_id, source, created_by, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', 'admin', ?, ?)
                    """,
                    (
                        subject, level, it["question"],
                        it["options"]["A"], it["options"]["B"],
                        it["options"]["C"], it["options"]["D"],
                        it["correct"], message.from_user.id, time.time(),
                    ),
                )
                inserted += 1
        await db.commit()

    await state.clear()
    text = (
        "✅ <b>Yuklash yakunlandi</b>\n\n"
        f"➕ Yangi savollar: <b>{inserted}</b> ta\n"
        f"✏️ Yangilangan: <b>{updated}</b> ta"
    )
    if errors:
        text += f"\n\n⚠️ Xatoliklar: {len(errors)} ta\n" + "\n".join(
            html.escape(e) for e in errors[:10]
        )
    await message.answer(text, reply_markup=bank_menu, parse_mode="HTML")


@dp.message(UploadQuestions.waiting_file)
async def qb_upload_hint(message: types.Message):
    await message.answer("Iltimos, .txt fayl yuboring (yoki «❌ Bekor qilish»):")


@dp.message(F.text == "✏️ Savolni tahrirlash")
async def qb_edit_start(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user):
        return
    await state.set_state(EditQuestion.waiting_id)
    await message.answer(
        "Tahrirlanadigan savolning <b>ID</b> raqamini kiriting:\n\n"
        "<i>ID ni «📤 Eksport» faylidan yoki «📊 Statistika»dan bilib olish mumkin.</i>",
        reply_markup=cancel_menu,
        parse_mode="HTML",
    )


async def show_question_editor(message: types.Message, qid: int) -> bool:
    async with connect_db() as db:
        async with db.execute(
            """
            SELECT id, subject, level, question, option_a, option_b, option_c, option_d, correct
            FROM question_bank WHERE id = ?
            """,
            (qid,),
        ) as cursor:
            row = await cursor.fetchone()

    if not row:
        await message.answer("⚠️ Bunday ID li savol topilmadi.")
        return False

    qid, subject, level, question, a, b, c, d, correct = row
    text = (
        f"<b>#{qid}</b> — {SUBJECT_NAMES.get(subject, subject)} / {LEVEL_NAMES.get(level, level)}\n\n"
        f"{html.escape(question)}\n\n"
        f"<b>A)</b> {html.escape(a)}\n"
        f"<b>B)</b> {html.escape(b)}\n"
        f"<b>C)</b> {html.escape(c)}\n"
        f"<b>D)</b> {html.escape(d)}\n\n"
        f"✅ To‘g‘ri javob: <b>{correct}</b>\n\n"
        "Qaysi maydonni o‘zgartiramiz?"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Savol matni", callback_data=f"qe:{qid}:question")],
            [
                InlineKeyboardButton(text="A varianti", callback_data=f"qe:{qid}:a"),
                InlineKeyboardButton(text="B varianti", callback_data=f"qe:{qid}:b"),
            ],
            [
                InlineKeyboardButton(text="C varianti", callback_data=f"qe:{qid}:c"),
                InlineKeyboardButton(text="D varianti", callback_data=f"qe:{qid}:d"),
            ],
            [
                InlineKeyboardButton(text="To‘g‘ri javob", callback_data=f"qe:{qid}:correct"),
                InlineKeyboardButton(text="Daraja", callback_data=f"qe:{qid}:level"),
            ],
            [InlineKeyboardButton(text="🗑 O‘chirish", callback_data=f"qe:{qid}:delete")],
        ]
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")
    return True


@dp.message(EditQuestion.waiting_id, F.text)
async def qb_edit_load(message: types.Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("⚠️ Iltimos, faqat raqamdan iborat ID kiriting:")
        return
    qid = int(message.text.strip())
    if await show_question_editor(message, qid):
        await state.update_data(edit_id=qid, edit_field=None)
        await state.set_state(EditQuestion.waiting_value)
    else:
        await message.answer("Qaytadan ID kiriting:")


@dp.callback_query(F.data.startswith("qe:"))
async def qb_edit_field(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user):
        await callback.answer("⛔️", show_alert=True)
        return

    _, sid, field = callback.data.split(":")
    qid = int(sid)

    if field == "cancel":
        await state.clear()
        await callback.message.edit_text("❌ Tahrirlash bekor qilindi.")
        await callback.answer()
        return

    if field == "delete":
        async with connect_db() as db:
            await db.execute("DELETE FROM question_bank WHERE id = ?", (qid,))
            await db.commit()
        await state.clear()
        await callback.message.edit_text(f"🗑 Savol #{qid} o‘chirildi.")
        await callback.message.answer("Savollar bazasi:", reply_markup=bank_menu)
        await callback.answer()
        return

    if field == "correct":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="A", callback_data=f"qec:{qid}:A"),
                    InlineKeyboardButton(text="B", callback_data=f"qec:{qid}:B"),
                    InlineKeyboardButton(text="C", callback_data=f"qec:{qid}:C"),
                    InlineKeyboardButton(text="D", callback_data=f"qec:{qid}:D"),
                ]
            ]
        )
        await callback.message.edit_text("Yangi to‘g‘ri javobni tanlang:", reply_markup=kb)
        await callback.answer()
        return

    if field == "level":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🟢 Oson", callback_data=f"qel:{qid}:easy"),
                    InlineKeyboardButton(text="🟡 O‘rtacha", callback_data=f"qel:{qid}:medium"),
                ],
                [InlineKeyboardButton(text="🔴 Qiyin", callback_data=f"qel:{qid}:hard")],
            ]
        )
        await callback.message.edit_text("Yangi darajani tanlang:", reply_markup=kb)
        await callback.answer()
        return

    prompts = {
        "question": "Yangi savol matnini yuboring:",
        "a": "Yangi A variantini yuboring:",
        "b": "Yangi B variantini yuboring:",
        "c": "Yangi C variantini yuboring:",
        "d": "Yangi D variantini yuboring:",
    }
    if field not in prompts:
        await callback.answer()
        return

    await state.update_data(edit_id=qid, edit_field=field)
    await state.set_state(EditQuestion.waiting_value)
    await callback.message.edit_text(prompts[field], reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="qe:0:cancel")]]
    ))
    await callback.answer()


@dp.message(EditQuestion.waiting_value, F.text)
async def qb_edit_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    qid = data.get("edit_id")
    field = data.get("edit_field")
    value = message.text.strip()

    columns = {
        "question": "question",
        "a": "option_a",
        "b": "option_b",
        "c": "option_c",
        "d": "option_d",
    }

    if not qid or field not in columns:
        if qid and field is None:
            await message.answer(
                "Avval yuqoridagi tugmalardan qaysi maydonni o‘zgartirishni tanlang:"
            )
            return
        await state.clear()
        await message.answer("⚠️ Sessiya eskirgan.", reply_markup=bank_menu)
        return

    if not value:
        await message.answer("Qiymat bo‘sh bo‘lmasligi kerak. Qaytadan:")
        return

    async with connect_db() as db:
        await db.execute(
            f"UPDATE question_bank SET {columns[field]} = ? WHERE id = ?",
            (value, qid),
        )
        await db.commit()

    await state.clear()
    await message.answer(f"✅ Savol #{qid} yangilandi.", reply_markup=bank_menu)


@dp.message(F.text == "🗑 Savolni o'chirish")
async def qb_delete_start(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user):
        return
    await state.set_state(DeleteQuestions.waiting_ids)
    await message.answer(
        "O‘chiriladigan savol ID raqamlarini vergul bilan yozing (masalan: 12, 15, 103):",
        reply_markup=cancel_menu,
    )


@dp.message(DeleteQuestions.waiting_ids, F.text)
async def qb_delete(message: types.Message, state: FSMContext):
    ids = [int(x) for x in re.findall(r"\d+", message.text)]
    if not ids:
        await message.answer("⚠️ Kamida bitta ID raqam kiriting:")
        return

    placeholders = ",".join("?" for _ in ids)
    async with connect_db() as db:
        cursor = await db.execute(
            f"DELETE FROM question_bank WHERE id IN ({placeholders})", ids
        )
        deleted = cursor.rowcount
        await db.commit()

    await state.clear()
    await message.answer(
        f"🗑 <b>{deleted}</b> ta savol o‘chirildi.",
        reply_markup=bank_menu,
        parse_mode="HTML",
    )


@dp.message(F.text == "📤 Eksport")
async def qb_export_start(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user):
        return
    await state.clear()
    await message.answer(
        "Qaysi fan savollarini eksport qilamiz?",
        reply_markup=get_bank_subject_kb("qx"),
    )


@dp.callback_query(F.data.startswith("qx:"))
async def qb_export_subject(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user):
        await callback.answer("⛔️", show_alert=True)
        return
    subject = callback.data.split(":")[1]
    if subject not in SUBJECT_NAMES:
        await callback.answer()
        return
    await callback.message.edit_text(
        f"{SUBJECT_NAMES[subject]} — darajani tanlang:",
        reply_markup=get_bank_level_kb(f"qxl:{subject}"),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("qxl:"))
async def qb_export_level(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user):
        await callback.answer("⛔️", show_alert=True)
        return
    _, subject, level = callback.data.split(":")
    if subject not in SUBJECT_NAMES or level not in LEVEL_NAMES:
        await callback.answer()
        return

    async with connect_db() as db:
        async with db.execute(
            """
            SELECT id, question, option_a, option_b, option_c, option_d, correct
            FROM question_bank
            WHERE subject = ? AND level = ?
            ORDER BY id
            """,
            (subject, level),
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await callback.answer("Bu bo‘limda savollar yo‘q.", show_alert=True)
        return

    blocks = []
    for qid, question, a, b, c, d, correct in rows:
        blocks.append(
            f"#ID {qid}\n{question}\nA) {a}\nB) {b}\nC) {c}\nD) {d}\nJavob: {correct}"
        )
    content = "\n\n".join(blocks)

    await callback.answer()
    await callback.message.answer_document(
        BufferedInputFile(
            content.encode("utf-8"), filename=f"{subject}_{level}.txt"
        ),
        caption=f"📤 {SUBJECT_NAMES[subject]} / {LEVEL_NAMES[level]} — {len(rows)} ta savol",
    )


@dp.callback_query(F.data.startswith("qec:"))
async def qb_edit_correct_save(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user):
        await callback.answer("⛔️", show_alert=True)
        return
    _, sid, correct = callback.data.split(":")
    async with connect_db() as db:
        await db.execute(
            "UPDATE question_bank SET correct = ? WHERE id = ?", (correct, int(sid))
        )
        await db.commit()
    await state.clear()
    await callback.message.edit_text(f"✅ Savol #{sid} to‘g‘ri javobi: <b>{correct}</b>", parse_mode="HTML")
    await callback.message.answer("Savollar bazasi:", reply_markup=bank_menu)
    await callback.answer()


@dp.callback_query(F.data.startswith("qel:"))
async def qb_edit_level_save(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user):
        await callback.answer("⛔️", show_alert=True)
        return
    _, sid, level = callback.data.split(":")
    if level not in LEVEL_NAMES:
        await callback.answer()
        return
    async with connect_db() as db:
        await db.execute(
            "UPDATE question_bank SET level = ? WHERE id = ?", (level, int(sid))
        )
        await db.commit()
    await state.clear()
    await callback.message.edit_text(
        f"✅ Savol #{sid} darajasi: <b>{LEVEL_NAMES[level]}</b>", parse_mode="HTML"
    )
    await callback.message.answer("Savollar bazasi:", reply_markup=bank_menu)
    await callback.answer()


# ==================== MILLIY CEFR ====================
CEFR_ROOT_TEXT = (
    "🇺🇿 <b>Milliy sertifikat (CEFR)</b>\n\n"
    "Rasmiy imtihon variantlari bo‘limi. Fanni tanlang:"
)

CEFR_SAMPLES = {
    "english": [
        ("If I ___ more time, I would learn another language.",
         ["have", "had", "will have", "would have"], "B"),
        ("The report ___ by the manager yesterday.",
         ["was written", "wrote", "has written", "is writing"], "A"),
        ("She is looking forward ___ from you.",
         ["to hear", "hearing", "to hearing", "hear"], "C"),
        ("The company decided to ___ the meeting until next week.",
         ["put off", "put out", "put up", "put on"], "A"),
        ("Neither of the answers ___ correct.",
         ["are", "were", "is", "have been"], "C"),
        ("By the time we arrived, the film ___.",
         ["already started", "had already started", "has already started", "was already starting"], "B"),
        ("She denied ___ the window.",
         ["break", "to break", "breaking", "broken"], "C"),
        ("You ___ have told me earlier — I could have helped.",
         ["should", "must", "would", "can"], "A"),
        ("Choose the synonym of «crucial»:",
         ["minor", "essential", "doubtful", "optional"], "B"),
        ("It was ___ a difficult exam that nobody passed.",
         ["so", "such", "too", "very"], "B"),
    ],
    "math": [
        ("Hisoblang: 2⁵ + 3².", ["39", "41", "43", "45"], "B"),
        ("3x − 7 = 11 tenglamani yeching.", ["4", "5", "6", "7"], "C"),
        ("240 sonining 25% ini toping.", ["60", "48", "72", "55"], "A"),
        ("Arifmetik progressiyada a₁ = 3, d = 4. a₁₀ ni toping.",
         ["41", "35", "43", "39"], "D"),
        ("Asosi 12 cm, balandligi 9 cm bo‘lgan uchburchakning yuzini toping.",
         ["48", "54", "60", "63"], "B"),
        ("Hisoblang: 3/4 + 1/4.", ["1/2", "1", "3/4", "5/4"], "B"),
        ("x² − 7x + 12 = 0 tenglamaning katta ildizini toping.",
         ["2", "3", "4", "6"], "C"),
        ("Radiusi 5 cm bo‘lgan aylananing yuzini toping (π = 3).",
         ["75", "72", "78", "80"], "A"),
        ("log₂ 32 ni hisoblang.", ["3", "4", "6", "5"], "D"),
        ("20% i 16 ga teng bo‘lgan sonni toping.", ["60", "72", "80", "96"], "C"),
    ],
}


def format_cefr_samples(subject: str) -> str:
    answers = []
    lines = [
        f"🧪 <b>Namuna savollar — {SUBJECT_NAMES[subject]}</b>",
        "",
        "<i>Rasmiy imtihon uslubidagi 10 ta namuna savol:</i>",
        "",
    ]
    for i, (question, options, correct) in enumerate(CEFR_SAMPLES[subject], start=1):
        lines.append(f"<b>{i}.</b> {html.escape(question)}")
        for letter, option in zip("ABCD", options):
            lines.append(f"{letter}) {html.escape(option)}")
        lines.append("")
        answers.append(f"{i}-{correct}")
    lines.append(f"✅ <b>Javoblar:</b> {', '.join(answers)}")
    return "\n".join(lines)


async def build_cefr_sub_view(subject: str, admin_user: bool):
    async with connect_db() as db:
        async with db.execute(
            "SELECT id, title FROM cefr_docs WHERE subject = ? ORDER BY id", (subject,)
        ) as cursor:
            rows = await cursor.fetchall()

    buttons = [
        [InlineKeyboardButton(text=title or f"Hujjat #{doc_id}", callback_data=f"cefr:{doc_id}")]
        for doc_id, title in rows
    ]
    buttons.append(
        [InlineKeyboardButton(text="🧪 Namuna savollar (10 ta)", callback_data=f"cefr_sample:{subject}")]
    )
    if admin_user:
        buttons.append(
            [InlineKeyboardButton(text="➕ Hujjat qo‘shish", callback_data=f"cefr_add:{subject}")]
        )
    buttons.append(
        [InlineKeyboardButton(text="🔙 Orqaga", callback_data="cefr_back")]
    )

    text = (
        f"🇺🇿 <b>Milliy sertifikat — {SUBJECT_NAMES[subject]}</b>\n\n"
        "Rasmiy imtihon variantlari. Hujjatni yuklab olib, qog‘ozda ishlashingiz mumkin."
    )
    if not rows:
        text += "\n\n<i>Hozircha hujjat yuklanmagan.</i>"
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


def get_cefr_root_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📘 Ingliz tili", callback_data="cefr_sub:english"),
                InlineKeyboardButton(text="➗ Matematika", callback_data="cefr_sub:math"),
            ]
        ]
    )


@dp.message(F.text == "🇺🇿 Milliy CEFR")
async def open_cefr(message: types.Message, state: FSMContext):
    await state.clear()
    if not await ensure_subscribed(message):
        return
    await message.answer(
        CEFR_ROOT_TEXT, reply_markup=get_cefr_root_kb(), parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("cefr_sub:"))
async def cefr_subsection(callback: types.CallbackQuery, state: FSMContext):
    if not await ensure_subscribed_cb(callback):
        return
    subject = callback.data.split(":", 1)[1]
    if subject not in SUBJECT_NAMES:
        await callback.answer("Bo‘lim topilmadi.", show_alert=True)
        return
    await state.clear()
    admin_user = await is_admin(callback.from_user)
    text, kb = await build_cefr_sub_view(subject, admin_user)
    await callback.answer()
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "cefr_back")
async def cefr_back(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    try:
        await callback.message.edit_text(
            CEFR_ROOT_TEXT, reply_markup=get_cefr_root_kb(), parse_mode="HTML"
        )
    except Exception:
        await callback.message.answer(
            CEFR_ROOT_TEXT, reply_markup=get_cefr_root_kb(), parse_mode="HTML"
        )


@dp.callback_query(F.data.startswith("cefr_sample:"))
async def cefr_sample(callback: types.CallbackQuery):
    if not await ensure_subscribed_cb(callback):
        return
    subject = callback.data.split(":", 1)[1]
    if subject not in CEFR_SAMPLES:
        await callback.answer("Bo‘lim topilmadi.", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer(
        format_cefr_samples(subject), parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("cefr:"))
async def cefr_send_doc(callback: types.CallbackQuery):
    if not await ensure_subscribed_cb(callback):
        return
    doc_id = int(callback.data.split(":")[1])
    async with connect_db() as db:
        async with db.execute(
            "SELECT title, file_id FROM cefr_docs WHERE id = ?", (doc_id,)
        ) as cursor:
            row = await cursor.fetchone()

    if not row or not row[1]:
        await callback.answer("Hujjat topilmadi.", show_alert=True)
        return

    title, file_id = row
    await callback.answer()
    await callback.message.answer_document(
        document=file_id,
        caption=(
            f"🇺🇿 <b>{html.escape(title)}</b>\n\n"
            "45 ta topshiriq. Javoblaringizni javoblar varaqasiga yozing."
        ),
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("cefr_add"))
async def cefr_add_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user):
        await callback.answer("⛔️", show_alert=True)
        return
    parts = callback.data.split(":", 1)
    subject = parts[1] if len(parts) > 1 and parts[1] in SUBJECT_NAMES else "math"
    await state.set_state(CefrAdd.waiting_doc)
    await state.update_data(subject=subject)
    await callback.answer()
    await callback.message.answer(
        f"📄 «{SUBJECT_NAMES[subject]}» bo‘limiga qo‘shiladigan PDF faylni yuboring:",
        reply_markup=cancel_menu,
    )


@dp.message(CefrAdd.waiting_doc, F.document)
async def cefr_add_doc(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user):
        return
    await state.update_data(
        file_id=message.document.file_id,
        file_name=message.document.file_name or "",
    )
    await state.set_state(CefrAdd.waiting_title)
    await message.answer("Hujjatga nom bering (masalan: «05.10.2025 — 1-smena»):")


@dp.message(CefrAdd.waiting_doc)
async def cefr_add_doc_hint(message: types.Message):
    await message.answer("Iltimos, PDF hujjat yuboring (yoki «❌ Bekor qilish»):")


@dp.message(CefrAdd.waiting_title, F.text)
async def cefr_add_title(message: types.Message, state: FSMContext):
    data = await state.get_data()
    title = message.text.strip()
    if not title:
        await message.answer("Nom bo‘sh bo‘lmasligi kerak. Qaytadan:")
        return

    subject = data.get("subject", "math")
    if subject not in SUBJECT_NAMES:
        subject = "math"

    async with connect_db() as db:
        await db.execute(
            "INSERT INTO cefr_docs (title, file_id, file_name, subject, added_at) VALUES (?, ?, ?, ?, ?)",
            (title, data.get("file_id", ""), data.get("file_name", ""), subject, time.time()),
        )
        await db.commit()

    await state.clear()
    admin_user = await is_admin(message.from_user)
    text, kb = await build_cefr_sub_view(subject, admin_user)
    await message.answer(
        f"✅ «{html.escape(title)}» hujjati qo‘shildi.\n\n{text}",
        reply_markup=kb,
        parse_mode="HTML",
    )


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
    await repair_db()
    await seed_question_bank()
    await start_web_server()
    # Orqa fonda test muddatlarini doimiy tekshiruvchi vazifa
    asyncio.create_task(check_test_deadlines_loop())
    # Render uxlab qolmasligi uchun avtomatik self-ping
    asyncio.create_task(render_keep_alive_loop())
    print("Rasch test boti yangilangan tizim bilan ishga tushdi...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())