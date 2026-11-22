"""2000 ta aralash matematika savolini generatsiya qiladi (javoblar avtomatik hisoblanadi).

Ishlatish:
    python tools/generate_questions.py

Natija:
    seed_generated.json — bot startupda bir marta bazaga yuklanadi.
"""
import json
import os
import random
import re
from fractions import Fraction

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_SEED = os.path.join(BASE_DIR, "seed_questions.json")
OUT_FILE = os.path.join(BASE_DIR, "seed_generated.json")

random.seed(20261122)

SUB_DIGITS = "₀₁₂₃₄₅₆₇₈₉"
SUP_DIGITS = "⁰¹²³⁴⁵⁶⁷⁸⁹"


def sub(n: int) -> str:
    return "".join(SUB_DIGITS[int(ch)] for ch in str(n))


def sup(n: int) -> str:
    return "".join(SUP_DIGITS[int(ch)] for ch in str(n))


def sfrac(x: Fraction) -> str:
    if x.denominator == 1:
        return str(x.numerator)
    return f"{x.numerator}/{x.denominator}"


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def sgn(b: int) -> str:
    return f"+ {b}" if b >= 0 else f"- {abs(b)}"


def poly2(b: int, c: int) -> str:
    out = "x²"
    if b:
        out += f" {sgn(b)}x"
    if c:
        out += f" {sgn(c)}"
    return out + " = 0"


NZ = [v for v in range(-9, 10) if v]


def deltas_for(ans: int):
    if abs(ans) < 10:
        return [1, 2, 3, 4, 5, -1]
    if abs(ans) < 200:
        return [1, -1, 2, -2, 10, -10, 3]
    return [10, -10, 100, -100, 1, -1, 50, -50]


def nq(question: str, ans, deltas=None):
    ans = int(ans)
    ds = []
    for d in (deltas or deltas_for(ans)):
        v = ans + d
        if v != ans and v not in ds:
            ds.append(v)
        if len(ds) == 3:
            break
    if len(ds) < 3:
        for v in (ans + 1, ans - 1, ans + 2):
            if v != ans and v not in ds:
                ds.append(v)
            if len(ds) == 3:
                break
    return question, str(ans), [str(v) for v in ds]


def fq(question: str, value: Fraction, extras):
    correct = sfrac(value)
    ds = []
    for extra in extras:
        s = sfrac(extra) if isinstance(extra, Fraction) else str(extra)
        if s != correct and s not in ds:
            ds.append(s)
        if len(ds) == 3:
            break
    if len(ds) < 3:
        for extra in (value + 1, value - 1, value * 2):
            s = sfrac(extra)
            if s != correct and s not in ds:
                ds.append(s)
            if len(ds) == 3:
                break
    return question, correct, ds


def sq(question: str, correct: str, pool):
    ds = []
    for v in pool:
        if v != correct and v not in ds:
            ds.append(v)
        if len(ds) == 3:
            break
    return question, correct, ds


# ==================== EASY ====================

def e_arith():
    op = random.choice(["+", "-", "·", "÷"])
    if op == "+":
        a, b = random.randint(15, 99), random.randint(15, 99)
        return nq(f"Hisoblang: {a} + {b}", a + b)
    if op == "-":
        a = random.randint(50, 150)
        b = random.randint(10, a - 10)
        return nq(f"Hisoblang: {a} - {b}", a - b)
    if op == "·":
        a, b = random.randint(6, 19), random.randint(3, 12)
        return nq(f"Hisoblang: {a} · {b}", a * b)
    b = random.randint(2, 12)
    ans = random.randint(3, 15)
    return nq(f"Hisoblang: {b * ans} ÷ {b}", ans)


def e_percent():
    p = random.choice([10, 20, 25, 40, 50, 75])
    n = random.choice([80, 120, 160, 200, 240, 320, 400, 480, 600, 800])
    return nq(f"{n} sonining {p} foizini toping.", n * p // 100)


def e_speed():
    v = random.choice([40, 45, 50, 60, 70, 80, 90])
    t = random.randint(2, 7)
    return nq(
        f"Avtomobil {v} km/soat tezlik bilan {t} soat yurdi. U qancha masofa bosib o‘tdi?",
        v * t,
    )


def e_factorial():
    n = random.randint(3, 6)
    fact = {3: 6, 4: 24, 5: 120, 6: 720}[n]
    return nq(f"{n}! ning qiymatini toping.", fact)


def e_power():
    base = random.choice([2, 3, 4, 5, 6])
    exp = random.randint(2, 5 if base < 5 else 3)
    return nq(f"{base}{sup(exp)} ifodaning qiymatini toping.", base ** exp)


def e_rect_p():
    a, b = random.randint(3, 25), random.randint(3, 25)
    return nq(
        f"Tomonlari {a} cm va {b} cm bo‘lgan to‘g‘ri to‘rtburchakning perimetrini toping.",
        2 * (a + b),
    )


def e_rect_s():
    a, b = random.randint(3, 25), random.randint(3, 25)
    return nq(
        f"Tomonlari {a} cm va {b} cm bo‘lgan to‘g‘ri to‘rtburchakning yuzini toping.",
        a * b,
    )


def e_linear():
    x = random.randint(-9, 12)
    a = random.randint(2, 9)
    b = random.randint(1, 15)
    c = a * x + b
    return nq(f"{a}x {sgn(b)} = {c} tenglamani yeching.", x)


def e_gcd():
    g = random.randint(2, 12)
    m, n = random.sample([2, 3, 4, 5, 7], 2)
    a, b = g * m, g * n
    return nq(f"{a} va {b} sonlarining EKUBini toping.", g, [1, -1, 2, 3, -2])


def e_lcm():
    a, b = random.sample([4, 5, 7, 9, 11, 13], 2)
    return nq(f"{a} va {b} sonlarining EKUKini toping.", a * b, [a + b, a * b - a, a * b // a])


TRIG_VALUES = {
    "sin 0°": "0", "sin 30°": "1/2", "sin 45°": "√2/2", "sin 60°": "√3/2",
    "sin 90°": "1", "sin 180°": "0",
    "cos 0°": "1", "cos 30°": "√3/2", "cos 45°": "√2/2", "cos 60°": "1/2",
    "cos 90°": "0", "cos 180°": "-1",
    "tg 0°": "0", "tg 30°": "√3/3", "tg 45°": "1", "tg 60°": "√3", "tg 180°": "0",
}


def e_trig():
    expr = random.choice(list(TRIG_VALUES.keys()))
    pool = list(set(TRIG_VALUES.values()))
    random.shuffle(pool)
    return sq(f"{expr} ning qiymatini toping.", TRIG_VALUES[expr], pool)


def e_average():
    mean = random.randint(5, 40)
    ds = [random.randint(-6, 6) for _ in range(3)]
    last = -sum(ds)
    if abs(last) > 60:
        return e_average()
    nums = [mean + d for d in ds] + [mean + last]
    if any(n < 0 for n in nums):
        return e_average()
    return nq(
        "Sonlarning o‘rtacha arifmetik qiymatini toping: " + ", ".join(map(str, nums)),
        mean,
    )


def e_prob():
    r, b = random.sample(range(1, 9), 2)[:2]
    t = r + b
    return fq(
        f"Qutida {r} ta qizil va {b} ta ko‘k shar bor. Tasodifiy olingan sharning qizil bo‘lish ehtimolini toping.",
        Fraction(r, t),
        [Fraction(r + 1, t), Fraction(r, t + 1), Fraction(b, t), Fraction(1, 2)],
    )


def e_units():
    kind = random.choice(["hour", "minute", "km"])
    n = random.randint(2, 15)
    if kind == "hour":
        return nq(f"{n} soat necha daqiqaga teng?", n * 60)
    if kind == "minute":
        return nq(f"{n} daqiqa necha sekundga teng?", n * 60)
    return nq(f"{n} km necha metrga teng?", n * 1000)


def e_fraction():
    m, n = random.sample([1, 2, 3, 4, 5], 2)
    m, n = min(m, n), max(m, n)
    k = random.randint(2, 6)
    a, b = m * k, n * k
    correct = f"{m}/{n}"
    ds = [f"{m}/{n + 1}", f"{n}/{m}", f"{m + 1}/{n}", f"{a}/{b}"]
    return sq(f"{a}/{b} kasrni qisqartiring.", correct, ds)


def e_squares():
    a, b = random.randint(2, 9), random.randint(2, 9)
    return nq(f"a² + b² ni hisoblang, bunda a = {a}, b = {b}.", a * a + b * b)


# ==================== MEDIUM ====================

def m_linear_neg():
    x = random.randint(-12, 12)
    a = random.randint(2, 9)
    b = random.randint(1, 20)
    c = a * x - b
    return nq(f"{a}x - {b} = {c} tenglamani yeching.", x)


def m_fx():
    a = random.randint(2, 9)
    b = random.randint(-9, 9)
    x0 = random.randint(-6, 6)
    return nq(
        f"f(x) = {a}x {sgn(b)} bo‘lsa, f({x0}) ni toping.", a * x0 + b
    )


def m_vieta_sum():
    p, q = random.sample(NZ, 2)
    return nq(
        f"{poly2(-(p + q), p * q)} tenglamaning ildizlari yig‘indisini toping.",
        p + q,
    )


def m_vieta_product():
    p, q = random.sample(NZ, 2)
    return nq(
        f"{poly2(-(p + q), p * q)} tenglamaning ildizlari ko‘paytmasini toping.",
        p * q,
    )


def m_discriminant():
    p, q = random.sample(NZ, 2)
    return nq(
        f"{poly2(-(p + q), p * q)} tenglamaning diskriminantini toping.",
        (p - q) ** 2,
    )


def m_ap_n():
    a1 = random.randint(-5, 12)
    d = random.randint(2, 7)
    n = random.randint(10, 20)
    return nq(
        f"Arifmetik progressiyada a{sub(1)} = {a1}, d = {d} bo‘lsa, a{sub(n)} ni toping.",
        a1 + (n - 1) * d,
    )


def m_ap_sum():
    a1 = random.randint(1, 12)
    d = random.randint(2, 7)
    n = random.choice([4, 6, 8, 10, 12])
    return nq(
        f"Arifmetik progressiyada a{sub(1)} = {a1}, d = {d} bo‘lsa, dastlabki {n} ta had yig‘indisini toping.",
        n * (2 * a1 + (n - 1) * d) // 2,
    )


def m_gp_n():
    b1 = random.randint(2, 5)
    q = random.choice([2, 3])
    n = random.randint(4, 6)
    return nq(
        f"Geometrik progressiyada b{sub(1)} = {b1}, q = {q} bo‘lsa, b{sub(n)} ni toping.",
        b1 * q ** (n - 1),
    )


def m_percent_inc():
    n = random.choice([200, 300, 400, 500, 600, 800, 900])
    p = random.choice([10, 15, 20, 25, 30, 40, 50])
    return nq(
        f"Mahsulot narxi {n} so‘m edi. U {p}% ga oshdi. Yangi narxni toping.",
        n + n * p // 100,
    )


def m_speed_time():
    v = random.choice([40, 50, 60, 70, 80, 90])
    t = random.randint(2, 8)
    return nq(
        f"Avtomobil {v} km/soat tezlikda {v * t} km masofani qancha soatda bosib o‘tadi?",
        t,
    )


def m_sumdiff():
    x = random.randint(20, 60)
    y = random.randint(2, x - 1)
    s, d = x + y, x - y
    return nq(
        f"Ikki sonning yig‘indisi {s}, ayirmasi {d}. Katta sonni toping.", x
    )


def m_gcd2():
    g = random.randint(3, 20)
    m, n = random.sample([4, 5, 7, 9, 11], 2)
    a, b = g * m, g * n
    return nq(f"{a} va {b} sonlarining EKUBini toping.", g)


def m_circle():
    r = random.randint(2, 12)
    return nq(
        f"Radiusi {r} cm bo‘lgan aylananing yuzini toping (π = 3).", 3 * r * r
    )


def m_triangle():
    a = random.randint(4, 30)
    h = 2 * random.randint(2, 12)
    return nq(
        f"Asosi {a} cm, balandligi {h} cm bo‘lgan uchburchakning yuzini toping.",
        a * h // 2,
    )


def m_mean5():
    for _ in range(20):
        nums = [random.randint(2, 60) for _ in range(5)]
        total = sum(nums)
        if total % 5 == 0:
            return nq(
                "Sonlarning o‘rtacha arifmetik qiymatini toping: "
                + ", ".join(map(str, nums)),
                total // 5,
            )
    return m_mean5()


def m_median5():
    nums = random.sample(range(2, 70), 5)
    return nq(
        "Sonlarning medianasini toping: " + ", ".join(map(str, nums)),
        sorted(nums)[2],
    )


def m_diff_sq():
    a, b = random.randint(3, 15), random.randint(2, 12)
    return nq(
        f"a² - b² ni hisoblang, bunda a = {a}, b = {b}.", a * a - b * b
    )


def m_log():
    base, n, val = random.choice([(2, 4, 2), (2, 8, 3), (2, 16, 4), (2, 32, 5),
                                  (2, 64, 6), (3, 9, 2), (3, 27, 3), (3, 81, 4)])
    return nq(f"log{sub(base)} {n} ni hisoblang.", val)


def m_ratio():
    a, b = random.sample([2, 3, 4, 5], 2)
    k = random.randint(4, 15)
    total = k * (a + b)
    return nq(
        f"{total} so‘m {a}:{b} nisbatda bo‘lib olindi. Katta ulushni toping.",
        max(a, b) * k,
    )


def m_expmul():
    base = random.choice([2, 3])
    e1, e2 = random.randint(2, 5), random.randint(2, 4)
    return nq(
        f"{base}{sup(e1)} · {base}{sup(e2)} ni hisoblang.", base ** (e1 + e2)
    )


# ==================== HARD ====================

def h_root():
    p, q = random.sample(NZ, 2)
    return nq(
        f"{poly2(-(p + q), p * q)} tenglamaning katta ildizini toping.",
        max(p, q),
    )


def h_derivative():
    a = random.randint(2, 6)
    b = random.randint(-9, 9)
    c = random.randint(-5, 5)
    x0 = random.randint(-5, 5)
    return nq(
        f"f(x) = {a}x² {sgn(b)}x {sgn(c)} bo‘lsa, f′({x0}) ni toping.",
        2 * a * x0 + b,
    )


def h_integral():
    a = random.randint(2, 6)
    b = random.randint(-5, 5)
    k = random.choice([2, 4, 6])
    return nq(
        f"Hisoblang: ∫₀{sup(k)} ({a}x {sgn(b)}) dx",
        a * k * k // 2 + b * k,
    )


def h_logeq():
    k = random.randint(2, 6)
    return nq(f"log₂ x = {k} bo‘lsa, x ni toping.", 2 ** k)


TRIG_COMBOS = [
    ("2sin30° + cos60°", Fraction(3, 2)),
    ("sin30° + cos60°", Fraction(1)),
    ("2cos60° + sin90°", Fraction(2)),
    ("2sin90° - cos0°", Fraction(1)),
    ("4cos60°", Fraction(2)),
    ("2tg45°", Fraction(2)),
    ("sin30° · cos60°", Fraction(1, 4)),
    ("cos60° · sin90°", Fraction(1, 2)),
    ("sin²30° + cos²30°", Fraction(1)),
    ("2sin45° · cos45°", Fraction(1)),
]


def h_trigcombo():
    expr, value = random.choice(TRIG_COMBOS)
    extras = [value + Fraction(1, 2), value - Fraction(1, 2), value * 2,
              value + 1, Fraction(1, 2), Fraction(3, 2)]
    return fq(f"Hisoblang: {expr}", value, extras)


def h_sphere():
    r = random.randint(2, 5)
    return nq(
        f"Radiusi {r} cm bo‘lgan sharning hajmini toping (π = 3).", 4 * r ** 3
    )


def h_cone():
    r = random.randint(2, 6)
    h = random.randint(3, 10)
    return nq(
        f"Asos radiusi {r} cm va balandligi {h} cm bo‘lgan konusning hajmini toping (π = 3).",
        r * r * h,
    )


def h_cylinder():
    r = random.randint(2, 6)
    h = random.randint(3, 10)
    return nq(
        f"Silindrning asos radiusi {r} cm, balandligi {h} cm. Hajmini toping (π = 3).",
        3 * r * r * h,
    )


def h_comb():
    n = random.randint(5, 12)
    return nq(
        f"{n} ta elementdan 2 tasini necha xil usulda tanlash mumkin?",
        n * (n - 1) // 2,
    )


def h_perm():
    n = random.randint(4, 6)
    fact = {4: 24, 5: 120, 6: 720}[n]
    return nq(
        f"{n} ta kitobni javonga necha xil tartibda joylashtirish mumkin?", fact
    )


def h_dice():
    k = random.randint(3, 11)
    counts = {2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6, 8: 5, 9: 4, 10: 3, 11: 2, 12: 1}
    cnt = counts[k]
    extras = [Fraction(cnt, 12), Fraction(cnt + 1, 36), Fraction(cnt - 1, 36) if cnt > 1 else Fraction(1, 36),
              Fraction(1, 6), Fraction(cnt + 2, 36)]
    return fq(
        f"Ikki kubikni tashlashda tushgan ochkolar yig‘indisi {k} ga teng bo‘lish ehtimolini toping.",
        Fraction(cnt, 36),
        extras,
    )


def h_balls2():
    r = random.randint(3, 6)
    b = random.randint(2, 5)
    t = r + b
    value = Fraction(r * (r - 1), t * (t - 1))
    extras = [Fraction(r, t), Fraction(r * r, t * t), Fraction(r - 1, t - 1),
              Fraction(1, 2), Fraction(r, t - 1)]
    return fq(
        f"Qutida {r} ta qizil va {b} ta ko‘k shar bor. Tasodifiy 2 ta shar olinsa, ikkalasi ham qizil bo‘lish ehtimolini toping.",
        value,
        extras,
    )


def h_ap_d():
    a1 = random.randint(2, 10)
    d = random.randint(2, 6)
    a5 = a1 + 4 * d
    a9 = a1 + 8 * d
    return nq(
        f"Arifmetik progressiyada a{sub(5)} = {a5}, a{sub(9)} = {a9} bo‘lsa, d ni toping.",
        d,
    )


def h_sqdiff():
    x = random.randint(6, 18)
    y = random.randint(2, x - 1)
    return nq(
        f"Ikki sonning yig‘indisi {x + y}, ayirmasi {x - y}. Shu sonlar kvadratlarining ayirmasini toping.",
        x * x - y * y,
    )


WORK_PAIRS = [(6, 3), (12, 4), (10, 15), (20, 5), (12, 6), (18, 9), (30, 6), (8, 24), (14, 35)]


def h_work():
    a, b = random.choice(WORK_PAIRS)
    return nq(
        f"Ishni birinchi ishchi {a} kunda, ikkinchisi {b} kunda bajaradi. Ular birgalikda necha kunda bajarishadi?",
        a * b // (a + b),
    )


def h_ineq():
    a = random.randint(2, 5)
    k = random.randint(-5, 8)
    b = random.randint(1, 9)
    c = a * k + b
    correct = f"x > {k}"
    ds = [f"x < {k}", f"x > {k - 1}", f"x ≥ {k}", f"x > {k + 1}"]
    return sq(
        f"{a}x {sgn(b)} > {c} tengsizlikni yeching.", correct, ds
    )


def h_simplify():
    a, b = random.randint(2, 12), random.randint(1, 10)
    return nq(
        f"(a + b)² - (a - b)² ni hisoblang, bunda a = {a}, b = {b}.", 4 * a * b
    )


def h_fvalue():
    b = random.randint(-8, 8)
    c = random.randint(-9, 9)
    x0 = random.randint(-6, 6)
    return nq(
        f"f(x) = x² {sgn(b)}x {sgn(c)} bo‘lsa, f({x0}) ni toping.",
        x0 * x0 + b * x0 + c,
    )


def h_percent_find():
    n = random.choice([100, 200, 300, 400, 500, 600, 800, 900])
    p = random.choice([10, 20, 25, 40, 50])
    q = n * p // 100
    return nq(
        f"Sonning {p}% i {q} ga teng bo‘lsa, shu sonni toping.", n
    )


EASY_BUILDERS = [
    (e_arith, 26), (e_percent, 10), (e_speed, 8), (e_factorial, 5),
    (e_power, 8), (e_rect_p, 6), (e_rect_s, 6), (e_linear, 12),
    (e_gcd, 6), (e_lcm, 4), (e_trig, 12), (e_average, 5),
    (e_prob, 5), (e_units, 6), (e_fraction, 5), (e_squares, 5),
]

MEDIUM_BUILDERS = [
    (m_linear_neg, 8), (m_fx, 8), (m_vieta_sum, 7), (m_vieta_product, 5),
    (m_discriminant, 5), (m_ap_n, 10), (m_ap_sum, 6), (m_gp_n, 6),
    (m_percent_inc, 8), (m_speed_time, 7), (m_sumdiff, 7), (m_gcd2, 5),
    (m_circle, 5), (m_triangle, 5), (m_mean5, 5), (m_median5, 4),
    (m_diff_sq, 6), (m_log, 8), (m_ratio, 6), (m_expmul, 6),
]

HARD_BUILDERS = [
    (h_root, 8), (h_derivative, 8), (h_integral, 6), (h_logeq, 6),
    (h_trigcombo, 8), (h_sphere, 4), (h_cone, 4), (h_cylinder, 4),
    (h_comb, 7), (h_perm, 5), (h_dice, 8), (h_balls2, 6),
    (h_ap_d, 7), (h_sqdiff, 5), (h_work, 6), (h_ineq, 8),
    (h_simplify, 5), (h_fvalue, 5), (h_percent_find, 6),
]


def pick(builders):
    total = sum(w for _, w in builders)
    r = random.uniform(0, total)
    acc = 0
    for fn, w in builders:
        acc += w
        if r <= acc:
            return fn
    return builders[-1][0]


def build_level(level, target, banned):
    items = []
    attempts = 0
    while len(items) < target and attempts < target * 80:
        attempts += 1
        builder = pick({"easy": EASY_BUILDERS, "medium": MEDIUM_BUILDERS, "hard": HARD_BUILDERS}[level])
        question, correct, ds = builder()
        question = question.strip()
        if not question or len(question) > 600:
            continue
        key = norm(question)
        if key in banned:
            continue

        opts = [correct]
        for d in ds:
            if d != correct and d not in opts:
                opts.append(d)
        if len(opts) < 4:
            continue
        opts = opts[:4]
        random.shuffle(opts)

        banned.add(key)
        items.append({
            "subject": "math",
            "level": level,
            "question": question,
            "option_a": opts[0],
            "option_b": opts[1],
            "option_c": opts[2],
            "option_d": opts[3],
            "correct": "ABCD"[opts.index(correct)],
            "source": "generated",
        })
    return items


def main():
    banned = set()
    if os.path.exists(BASE_SEED):
        with open(BASE_SEED, encoding="utf-8") as f:
            for it in json.load(f):
                banned.add(norm(it["question"]))

    easy = build_level("easy", 700, banned)
    medium = build_level("medium", 700, banned)
    hard = build_level("hard", 600, banned)

    items = easy + medium + hard
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)

    from collections import Counter
    print("yozildi:", OUT_FILE)
    print("jami:", len(items), Counter(i["level"] for i in items))
    print("noyob:", len({norm(i["question"]) for i in items}))


if __name__ == "__main__":
    main()
