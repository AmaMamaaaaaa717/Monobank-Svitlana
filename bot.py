import asyncio
import html
import logging
import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from render import make_chart, make_receipt, money

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("monobot")

# ───────────── Налаштування (змінні середовища) ─────────────
TG_TOKEN = os.environ["TG_TOKEN"]
MONO_TOKEN = os.environ["MONO_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
SECRET = os.environ["WEBHOOK_SECRET"]
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "bot.db")
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Kyiv"))
ALERT_UAH = float(os.getenv("ALERT_THRESHOLD_UAH", "5000"))   # «велика операція»
NIGHT_MIN_UAH = float(os.getenv("NIGHT_MIN_UAH", "300"))      # нічні списання від цієї суми
NEW_MERCHANT_UAH = float(os.getenv("NEW_MERCHANT_UAH", "1000"))  # новий мерчант від цієї суми
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "21"))             # година вечірнього звіту

PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
if not PUBLIC_URL and os.getenv("RAILWAY_PUBLIC_DOMAIN"):
    PUBLIC_URL = "https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"]

MONO = "https://api.monobank.ua"

# ───────────── Категорії ─────────────
CAT_EMOJI = {
    "їжа": "🍔", "продукти": "🛒", "транспорт": "🚕", "авто": "🚗", "телефон": "📱",
    "медицина": "💊", "розваги": "🎬", "одяг": "👕", "покупки": "🛍", "краса": "💇",
    "освіта": "📚", "подорожі": "✈️", "перекази": "💸", "готівка": "🏧", "інше": "📦",
}


def category(mcc) -> str:
    m = int(mcc or 0)
    if m in (5411, 5422, 5441, 5451, 5462, 5499):
        return "продукти"
    if m in (5811, 5812, 5813, 5814):
        return "їжа"
    if m in (4111, 4112, 4121, 4131, 4011, 4784, 4789):
        return "транспорт"
    if m in (5541, 5542, 5511, 5521, 5531, 5532, 5533, 5599, 7512, 7523, 7531, 7538, 7549):
        return "авто"
    if m in (4812, 4814, 4816, 4899, 4900):
        return "телефон"
    if m in (5912, 5122, 5047) or 8011 <= m <= 8099:
        return "медицина"
    if m in (5815, 5816, 5817, 5818, 5735) or 7832 <= m <= 7841 or 7911 <= m <= 7999:
        return "розваги"
    if 5611 <= m <= 5699:
        return "одяг"
    if m in (7230, 7297, 7298):
        return "краса"
    if m in (8211, 8220, 8241, 8244, 8249, 8299, 5942, 5943):
        return "освіта"
    if 3000 <= m <= 3999 or m in (4511, 4722, 7011, 4582):
        return "подорожі"
    if m in (4829, 6012, 6050, 6051, 6538, 6540):
        return "перекази"
    if m in (6010, 6011):
        return "готівка"
    if 5200 <= m <= 5399 or 5700 <= m <= 5799 or 5900 <= m <= 5999:
        return "покупки"
    return "інше"


# ───────────── База даних ─────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS tx(
  n INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT UNIQUE, account TEXT, time INTEGER,
  description TEXT, dlc TEXT, mcc INTEGER,
  amount INTEGER, op_amount INTEGER, op_cur INTEGER, cur INTEGER,
  cashback INTEGER, commission INTEGER, balance INTEGER,
  comment TEXT, counter TEXT, receipt TEXT);
CREATE INDEX IF NOT EXISTS tx_time ON tx(time);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS limits(cat TEXT PRIMARY KEY, amount REAL);
CREATE TABLE IF NOT EXISTS alerts(k TEXT PRIMARY KEY);
"""
)

DEFAULTS = {"guard": "1", "reports": "1", "quiet": "0"}


def setting(k: str) -> str:
    r = db.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r[0] if r else DEFAULTS.get(k, "")


def set_setting(k: str, v: str):
    db.execute("INSERT OR REPLACE INTO settings(k, v) VALUES(?, ?)", (k, v))
    db.commit()


def to_row(it: dict, acc_id: str, cur: int) -> dict:
    desc = it.get("description", "") or ""
    return {
        "id": it["id"], "account": acc_id, "time": it["time"],
        "description": desc, "dlc": desc.lower(), "mcc": it.get("mcc", 0),
        "amount": it["amount"],
        "op_amount": it.get("operationAmount", it["amount"]),
        "op_cur": it.get("currencyCode", cur), "cur": cur,
        "cashback": it.get("cashbackAmount", 0),
        "commission": it.get("commissionRate", 0),
        "balance": it.get("balance"),
        "comment": it.get("comment", "") or "",
        "counter": it.get("counterName", "") or "",
        "receipt": it.get("receiptId", "") or "",
    }


def insert_tx(r: dict):
    c = db.execute(
        """INSERT OR IGNORE INTO tx(id, account, time, description, dlc, mcc, amount,
           op_amount, op_cur, cur, cashback, commission, balance, comment, counter, receipt)
           VALUES(:id, :account, :time, :description, :dlc, :mcc, :amount,
           :op_amount, :op_cur, :cur, :cashback, :commission, :balance, :comment,
           :counter, :receipt)""",
        r,
    )
    db.commit()
    return c.lastrowid if c.rowcount else None


# ───────────── Стан та Monobank API ─────────────
state = {"ts": 0.0, "accounts": {}, "bf": "idle"}
session: aiohttp.ClientSession
bot: Bot


async def mono(method: str, path: str, **kw):
    async with session.request(
        method, MONO + path, headers={"X-Token": MONO_TOKEN}, **kw
    ) as r:
        if r.status == 429:
            raise RuntimeError("Монобанк просить почекати (ліміт 1 запит/60с).")
        if r.status != 200:
            raise RuntimeError(f"Монобанк повернув {r.status}: {await r.text()}")
        return await r.json(content_type=None)


async def refresh(force: bool = False):
    if not force and time.time() - state["ts"] < 61 and state["accounts"]:
        return
    data = await mono("GET", "/personal/client-info")
    accs = {}
    for a in data.get("accounts", []):
        pan = (a.get("maskedPan") or [""])[0]
        accs[a["id"]] = {
            "label": f'{a.get("type", "card")} {pan}'.strip(),
            "balance": a["balance"], "credit": a.get("creditLimit", 0),
            "currency": a["currencyCode"],
        }
    for j in data.get("jars", []):
        accs[j["id"]] = {
            "label": f'🫙 {j.get("title", "банка")}',
            "balance": j["balance"], "credit": 0, "currency": j["currencyCode"],
        }
    state["accounts"] = accs
    state["ts"] = time.time()


_stmt_lock = asyncio.Lock()
_stmt_last = 0.0


async def statement(acc: str, t_from: int, t_to: int):
    """Виписка з дотриманням ліміту 1 запит / 60 с."""
    global _stmt_last
    async with _stmt_lock:
        wait = 61 - (time.time() - _stmt_last)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            return await mono("GET", f"/personal/statement/{acc}/{t_from}/{t_to}")
        finally:
            _stmt_last = time.time()


async def backfill():
    """Підтягує історію за 30 днів у базу (для статистики, пошуку, /last)."""
    state["bf"] = "running"
    for acc_id, acc in list(state["accounts"].items()):
        t_to = int(time.time())
        t_from = t_to - 30 * 86400
        while True:
            items = None
            for _ in range(3):
                try:
                    items = await statement(acc_id, t_from, t_to)
                    break
                except Exception as e:
                    log.warning("backfill %s: %s", acc_id, e)
            if not items:
                break
            for it in items:
                insert_tx(to_row(it, acc_id, acc["currency"]))
            if len(items) < 500:
                break
            t_to = items[-1]["time"]
    state["bf"] = "done"
    log.info("Backfill done")
    try:
        await bot.send_message(OWNER_ID, "📥 Історію за 30 днів завантажено.")
    except Exception:
        pass


def bf_note() -> str:
    if state["bf"] == "running":
        return "\n\n⏳ <i>Історія ще завантажується, цифри можуть бути неповними.</i>"
    return ""


def acc_label(acc_id: str) -> str:
    return state["accounts"].get(acc_id, {}).get("label", "рахунок")


# ───────────── Час ─────────────
def period_start(p: str) -> int:
    now = datetime.now(TZ)
    if p == "day":
        d = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif p == "week":
        d = now - timedelta(days=7)
    else:
        d = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp())


PERIOD_NAME = {"day": "Сьогодні", "week": "Останні 7 днів", "month": "Цей місяць"}
PERIOD_ALIAS = {"день": "day", "сьогодні": "day", "тиждень": "week", "місяць": "month",
                "day": "day", "week": "week", "month": "month"}


def short_dt(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M")


def is_night() -> bool:
    h = datetime.now(TZ).hour
    return h >= 23 or h < 7


# ───────────── Підозрілі операції ─────────────
def analyze(r: dict) -> list:
    flags = []
    if r["cur"] == 980 and abs(r["amount"]) / 100 >= ALERT_UAH:
        flags.append("⚠️ Велика операція!")
    if r["amount"] >= 0 or setting("guard") != "1":
        return flags

    uah = abs(r["amount"]) / 100 if r["cur"] == 980 else 0
    hour = datetime.fromtimestamp(r["time"], TZ).hour
    if hour < 6 and uah >= NIGHT_MIN_UAH:
        flags.append("🌙 Нічна операція")

    total = db.execute("SELECT COUNT(*) FROM tx").fetchone()[0]
    if total >= 20 and uah >= NEW_MERCHANT_UAH:
        seen = db.execute("SELECT 1 FROM tx WHERE dlc=? LIMIT 1", (r["dlc"],)).fetchone()
        if not seen:
            flags.append("🆕 Новий мерчант / отримувач")

    if total >= 20 and r["op_cur"] != 980:
        seen = db.execute("SELECT 1 FROM tx WHERE op_cur=? LIMIT 1", (r["op_cur"],)).fetchone()
        if not seen:
            flags.append(f"🌍 Платіж у новій валюті ({r['op_cur']})")

    recent = db.execute(
        "SELECT COUNT(*) FROM tx WHERE amount<0 AND time>=? AND time<=?",
        (r["time"] - 600, r["time"]),
    ).fetchone()[0]
    if recent >= 2:
        flags.append("⚡ Кілька списань поспіль")
    return flags


# ───────────── Форматування ─────────────
def fmt_tx(r: dict, flags: list) -> str:
    cur = r["cur"]
    cat = category(r["mcc"])
    lines = [f"<b>{f}</b>" for f in flags]
    icon = "🔴" if r["amount"] < 0 else "🟢"
    lines.append(f'{CAT_EMOJI[cat]} {icon} <b>{money(r["amount"], cur)}</b>')
    lines.append(html.escape(r["description"]))
    lines.append(f"<i>{cat}</i>")
    if r["comment"]:
        lines.append("💬 " + html.escape(r["comment"]))
    if r["op_cur"] != cur:
        lines.append(f'Сума операції: {money(r["op_amount"], r["op_cur"])}')
    if r["cashback"]:
        lines.append(f'Кешбек: {money(r["cashback"], cur)}')
    if r["balance"] is not None:
        lines.append(f'Баланс: {money(r["balance"], cur)}')
    lines.append(f'<i>{html.escape(acc_label(r["account"]))}</i>')
    return "\n".join(lines)


def receipt_kb(n: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🧾 Квитанція", callback_data=f"r:{n}")]]
    )


def list_view(rows: list, title: str):
    lines = [title]
    btns = []
    for i, r in enumerate(rows, 1):
        e = CAT_EMOJI[category(r["mcc"])]
        lines.append(
            f'{i}. {e} <b>{money(r["amount"], r["cur"])}</b> · '
            f'{html.escape(r["description"][:35])} · {short_dt(r["time"])}'
        )
        btns.append(InlineKeyboardButton(text=f"🧾{i}", callback_data=f'r:{r["n"]}'))
    kb = InlineKeyboardMarkup(inline_keyboard=[btns[i:i + 5] for i in range(0, len(btns), 5)])
    return "\n".join(lines), kb


# ───────────── Статистика ─────────────
def build_stats(p: str):
    start = period_start(p)
    rows = db.execute(
        "SELECT mcc, description, amount FROM tx WHERE time>=? AND cur=980", (start,)
    ).fetchall()
    spent = income = 0
    cats, merch = defaultdict(int), defaultdict(int)
    for mcc, desc, amt in rows:
        if amt < 0:
            spent += -amt
            cats[category(mcc)] += -amt
            merch[desc] += -amt
        else:
            income += amt
    lines = [f"📊 <b>{PERIOD_NAME[p]}</b>",
             f"🔴 Витрати: <b>{money(spent, 980)}</b>",
             f"🟢 Надходження: <b>{money(income, 980)}</b>"]
    chart = None
    if cats:
        lines.append("")
        ordered = sorted(cats.items(), key=lambda x: -x[1])
        for c, v in ordered:
            lines.append(f"{CAT_EMOJI[c]} {c} — {money(v, 980)} ({v * 100 // spent}%)")
        lines.append("\n<b>Топ витрат:</b>")
        for d, v in sorted(merch.items(), key=lambda x: -x[1])[:5]:
            lines.append(f"• {html.escape(d[:30])} — {money(v, 980)}")
        chart = [(c, v / 100) for c, v in ordered]
    lines.append("\n<i>Лише гривневі рахунки; перекази між своїми рахунками теж враховані.</i>")
    return "\n".join(lines) + bf_note(), chart, PERIOD_NAME[p]


async def send_stats(chat_id: int, p: str):
    text, chart, name = build_stats(p)
    await bot.send_message(chat_id, text)
    if chart:
        png = await asyncio.to_thread(make_chart, f"Витрати: {name}", chart)
        await bot.send_photo(chat_id, BufferedInputFile(png, "stats.png"))


# ───────────── Ліміти ─────────────
def month_spend():
    rows = db.execute(
        "SELECT mcc, amount FROM tx WHERE time>=? AND cur=980 AND amount<0",
        (period_start("month"),),
    ).fetchall()
    cats = defaultdict(int)
    for mcc, amt in rows:
        cats[category(mcc)] += -amt
    return cats, sum(cats.values())


async def check_limits(r: dict):
    if r["amount"] >= 0 or r["cur"] != 980:
        return
    cats, total = month_spend()
    month_key = datetime.now(TZ).strftime("%Y-%m")
    cat = category(r["mcc"])
    for key, spent in ((cat, cats[cat]), ("всього", total)):
        lim = db.execute("SELECT amount FROM limits WHERE cat=?", (key,)).fetchone()
        if not lim:
            continue
        ratio = spent / (lim[0] * 100)
        level = 100 if ratio >= 1 else 80 if ratio >= 0.8 else 0
        if not level:
            continue
        c = db.execute("INSERT OR IGNORE INTO alerts(k) VALUES(?)",
                       (f"{month_key}:{key}:{level}",))
        db.commit()
        if c.rowcount:
            e = CAT_EMOJI.get(key, "💰")
            word = "перевищено" if level == 100 else "використано 80%"
            await bot.send_message(
                OWNER_ID,
                f"🎯 Ліміт {e} <b>{key}</b>: {word}\n"
                f"{money(spent, 980)} із {money(int(lim[0] * 100), 980)} за місяць",
            )


def limits_text() -> str:
    lims = db.execute("SELECT cat, amount FROM limits ORDER BY cat").fetchall()
    if not lims:
        return ("🎯 Лімітів поки немає.\n\nПриклад: <code>/limit їжа 8000</code>\n"
                "Загальний: <code>/limit всього 30000</code>\n"
                "Прибрати: <code>/limit їжа 0</code>\n\n"
                "Категорії: " + ", ".join(CAT_EMOJI))
    cats, total = month_spend()
    out = ["🎯 <b>Ліміти на місяць</b>"]
    for cat, amount in lims:
        spent = total if cat == "всього" else cats[cat]
        pct = int(spent / (amount * 100) * 100)
        filled = min(10, pct // 10)
        bar = "▓" * filled + "░" * (10 - filled)
        out.append(f'{CAT_EMOJI.get(cat, "💰")} {cat}: {money(spent, 980)} / '
                   f'{money(int(amount * 100), 980)} ({pct}%)\n{bar}')
    return "\n\n".join(out)


# ───────────── Обробка подій Monobank ─────────────
async def handle_event(payload: dict):
    try:
        if payload.get("type") != "StatementItem":
            return
        d = payload["data"]
        it = d["statementItem"]
        acc = state["accounts"].get(d["account"])
        cur = acc["currency"] if acc else it.get("currencyCode", 980)
        r = to_row(it, d["account"], cur)
        flags = analyze(r)
        n = insert_tx(r)
        if n is None:
            return  # дубль (Монобанк міг повторити запит)
        if acc and r["balance"] is not None:
            acc["balance"] = r["balance"]
        silent = setting("quiet") == "1" and is_night() and not flags
        await bot.send_message(OWNER_ID, fmt_tx(r, flags), reply_markup=receipt_kb(n),
                               disable_notification=silent)
        await check_limits(r)
    except Exception:
        log.exception("event handling failed")


async def mono_verify(request: web.Request):
    return web.Response(text="ok")


async def mono_hook(request: web.Request):
    payload = await request.json()
    asyncio.create_task(handle_event(payload))
    return web.Response(text="ok")


# ───────────── Telegram: клавіатури ─────────────
def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="🧾 Останні")],
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="🔎 Пошук")],
            [KeyboardButton(text="🎯 Ліміти"), KeyboardButton(text="⚙️ Налаштування")],
        ],
        resize_keyboard=True,
    )


def settings_kb() -> InlineKeyboardMarkup:
    def b(k, name):
        on = setting(k) == "1"
        return [InlineKeyboardButton(text=f'{name}: {"УВІМК" if on else "ВИМК"}',
                                     callback_data=f"t:{k}")]
    return InlineKeyboardMarkup(inline_keyboard=[
        b("guard", "🛡 Підозрілі операції"),
        b("reports", "📰 Вечірні звіти"),
        b("quiet", "🌙 Без звуку вночі"),
    ])


def periods_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Сьогодні", callback_data="s:day"),
        InlineKeyboardButton(text="Тиждень", callback_data="s:week"),
        InlineKeyboardButton(text="Місяць", callback_data="s:month"),
    ]])


HELP = (
    "Привіт! Я надсилаю сповіщення про всі операції, а кнопки внизу — швидкий доступ.\n\n"
    "/balance — баланси\n"
    "/last [N] — останні операції (N — номер рахунку з /balance)\n"
    "/stats [день|тиждень|місяць] — статистика й діаграма\n"
    "/find текст — пошук за мерчантом\n"
    "/limit категорія сума — ліміт на місяць\n"
    "/limits — прогрес лімітів\n"
    "/settings — налаштування\n\n"
    "Під кожною операцією є кнопка «🧾 Квитанція»."
)

router = Router()
router.message.filter(F.from_user.id == OWNER_ID)
router.callback_query.filter(F.from_user.id == OWNER_ID)


@router.message(Command("start"))
@router.message(Command("help"))
async def cmd_start(m: Message):
    await m.answer(HELP, reply_markup=main_kb())


@router.message(Command("balance"))
@router.message(F.text == "💰 Баланс")
async def cmd_balance(m: Message):
    try:
        await refresh()
    except Exception as e:
        return await m.answer(html.escape(str(e)))
    lines = []
    for n, a in enumerate(state["accounts"].values(), 1):
        s = f'{n}. {a["label"]}: <b>{money(a["balance"], a["currency"])}</b>'
        if a["credit"]:
            s += f' (власні: {money(a["balance"] - a["credit"], a["currency"])})'
        lines.append(s)
    await m.answer("\n".join(lines) or "Рахунків не знайдено")


@router.message(Command("last"))
@router.message(F.text == "🧾 Останні")
async def cmd_last(m: Message, command: CommandObject | None = None):
    arg = (command.args or "").strip() if command else ""
    ids = list(state["accounts"])
    title = "🧾 <b>Останні операції</b>"
    if arg.isdigit() and 1 <= int(arg) <= len(ids):
        acc_id = ids[int(arg) - 1]
        rows = db.execute("SELECT * FROM tx WHERE account=? ORDER BY time DESC LIMIT 10",
                          (acc_id,)).fetchall()
        title += f" · {html.escape(acc_label(acc_id))}"
    else:
        rows = db.execute("SELECT * FROM tx ORDER BY time DESC LIMIT 10").fetchall()
    if not rows:
        return await m.answer("Операцій поки немає." + bf_note())
    text, kb = list_view([dict(r) for r in rows], title)
    await m.answer(text + bf_note(), reply_markup=kb)


@router.message(Command("stats"))
@router.message(F.text == "📊 Статистика")
async def cmd_stats(m: Message, command: CommandObject | None = None):
    arg = (command.args or "").strip().lower() if command else ""
    if arg in PERIOD_ALIAS:
        return await send_stats(m.chat.id, PERIOD_ALIAS[arg])
    await m.answer("За який період?", reply_markup=periods_kb())


@router.callback_query(F.data.startswith("s:"))
async def cb_stats(c: CallbackQuery):
    await c.answer()
    await send_stats(c.message.chat.id, c.data.split(":")[1])


@router.message(Command("find"))
async def cmd_find(m: Message, command: CommandObject | None = None):
    q = ((command.args if command else "") or "").strip().lower()
    if not q:
        return await m.answer("Напиши, що шукати: <code>/find Сільпо</code>")
    rows = [dict(r) for r in db.execute(
        "SELECT * FROM tx WHERE dlc LIKE ? ORDER BY time DESC LIMIT 200", (f"%{q}%",)
    ).fetchall()]
    if not rows:
        return await m.answer("Нічого не знайдено." + bf_note())
    spent = sum(-r["amount"] for r in rows if r["amount"] < 0 and r["cur"] == 980)
    text, kb = list_view(rows[:10], f"🔎 <b>{html.escape(q)}</b>: знайдено {len(rows)}, "
                                    f"витрачено {money(spent, 980)}")
    await m.answer(text + bf_note(), reply_markup=kb)


@router.message(F.text == "🔎 Пошук")
async def btn_find(m: Message):
    await m.answer("Напиши, що шукати: <code>/find Сільпо</code>")


@router.message(Command("limit"))
async def cmd_limit(m: Message, command: CommandObject | None = None):
    args = ((command.args if command else "") or "").split()
    if len(args) < 2:
        return await m.answer(limits_text())
    try:
        val = float(args[-1].replace(",", "."))
    except ValueError:
        return await m.answer("Сума має бути числом: <code>/limit їжа 8000</code>")
    cat = " ".join(args[:-1]).lower()
    if cat not in CAT_EMOJI and cat != "всього":
        return await m.answer("Невідома категорія. Доступні: всього, " + ", ".join(CAT_EMOJI))
    month_key = datetime.now(TZ).strftime("%Y-%m")
    db.execute("DELETE FROM alerts WHERE k LIKE ?", (f"{month_key}:{cat}:%",))
    if val <= 0:
        db.execute("DELETE FROM limits WHERE cat=?", (cat,))
    else:
        db.execute("INSERT OR REPLACE INTO limits(cat, amount) VALUES(?, ?)", (cat, val))
    db.commit()
    await m.answer(limits_text())


@router.message(Command("limits"))
@router.message(F.text == "🎯 Ліміти")
async def cmd_limits(m: Message):
    await m.answer(limits_text())


@router.message(Command("settings"))
@router.message(F.text == "⚙️ Налаштування")
async def cmd_settings(m: Message):
    await m.answer(
        "⚙️ <b>Налаштування</b>\n\n"
        "🛡 Підозрілі: нічні списання, нові мерчанти, нова валюта, серії списань\n"
        "📰 Вечірній звіт (щодня, у неділю ще й тижневий)\n"
        "🌙 Без звуку вночі (23:00–07:00), крім підозрілих",
        reply_markup=settings_kb(),
    )


@router.callback_query(F.data.startswith("t:"))
async def cb_toggle(c: CallbackQuery):
    k = c.data.split(":")[1]
    if k in DEFAULTS:
        set_setting(k, "0" if setting(k) == "1" else "1")
    await c.answer("Збережено")
    await c.message.edit_reply_markup(reply_markup=settings_kb())


@router.callback_query(F.data.startswith("r:"))
async def cb_receipt(c: CallbackQuery):
    row = db.execute("SELECT * FROM tx WHERE n=?", (int(c.data.split(":")[1]),)).fetchone()
    if not row:
        return await c.answer("Операцію не знайдено", show_alert=True)
    await c.answer("Малюю квитанцію…")
    r = dict(row)
    png = await asyncio.to_thread(
        make_receipt, r, acc_label(r["account"]), category(r["mcc"]), TZ
    )
    await bot.send_photo(c.message.chat.id, BufferedInputFile(png, "receipt.png"))


# ───────────── Звіти ─────────────
async def reports_loop():
    while True:
        await asyncio.sleep(30)
        try:
            now = datetime.now(TZ)
            if now.hour != REPORT_HOUR or setting("reports") != "1":
                continue
            key = now.strftime("%Y-%m-%d")
            if setting("last_report") == key:
                continue
            set_setting("last_report", key)
            await bot.send_message(OWNER_ID, "📰 <b>Вечірній звіт</b>")
            await send_stats(OWNER_ID, "day")
            if now.weekday() == 6:
                await send_stats(OWNER_ID, "week")
        except Exception:
            log.exception("report failed")


# ───────────── Запуск ─────────────
async def set_webhook():
    if not PUBLIC_URL:
        log.warning("PUBLIC_URL не задано — вебхук не встановлено")
        return
    await mono("POST", "/personal/webhook", json={"webHookUrl": f"{PUBLIC_URL}/mono/{SECRET}"})
    log.info("Webhook set")


async def main():
    global session, bot
    session = aiohttp.ClientSession()
    bot = Bot(TG_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    app = web.Application()
    app.router.add_get(f"/mono/{SECRET}", mono_verify)
    app.router.add_post(f"/mono/{SECRET}", mono_hook)
    app.router.add_get("/", mono_verify)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    try:
        await refresh(force=True)
        await set_webhook()
    except Exception:
        log.exception("startup setup failed")

    try:
        await bot.send_message(OWNER_ID, "✅ Бот запущено", reply_markup=main_kb())
    except Exception:
        log.exception("cannot message owner (перевір OWNER_ID)")

    tasks = [asyncio.create_task(reports_loop())]
    if state["accounts"]:
        tasks.append(asyncio.create_task(backfill()))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
