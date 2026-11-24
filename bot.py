import asyncio
import html
import logging
import os
import time

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("monobot")

TG_TOKEN = os.environ["TG_TOKEN"]
MONO_TOKEN = os.environ["MONO_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
SECRET = os.environ["WEBHOOK_SECRET"]
PORT = int(os.getenv("PORT", "8080"))
ALERT_UAH = float(os.getenv("ALERT_THRESHOLD_UAH", "5000"))

PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
if not PUBLIC_URL and os.getenv("RAILWAY_PUBLIC_DOMAIN"):
    PUBLIC_URL = "https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"]

MONO = "https://api.monobank.ua"
CUR = {980: "₴", 840: "$", 978: "€"}

state = {"ts": 0.0, "accounts": {}}
session: aiohttp.ClientSession
bot: Bot


def money(kop: int, code: int) -> str:
    return f"{kop / 100:,.2f}".replace(",", " ") + f" {CUR.get(code, code)}"


async def mono(method: str, path: str, **kw):
    async with session.request(
        method, MONO + path, headers={"X-Token": MONO_TOKEN}, **kw
    ) as r:
        if r.status == 429:
            raise RuntimeError("Монобанк просить почекати (ліміт 1 запит/60с). Спробуй за хвилину.")
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
            "balance": a["balance"],
            "credit": a.get("creditLimit", 0),
            "currency": a["currencyCode"],
        }
    for j in data.get("jars", []):
        accs[j["id"]] = {
            "label": f'🫙 {j.get("title", "банка")}',
            "balance": j["balance"],
            "credit": 0,
            "currency": j["currencyCode"],
        }
    state["accounts"] = accs
    state["ts"] = time.time()


def fmt_item(it: dict, acc: dict) -> str:
    amt, cur = it["amount"], acc["currency"]
    lines = [
        f'{"🔴" if amt < 0 else "🟢"} <b>{money(amt, cur)}</b>',
        html.escape(it.get("description", "")),
    ]
    if it.get("comment"):
        lines.append("💬 " + html.escape(it["comment"]))
    if it.get("currencyCode", cur) != cur:
        lines.append(f'Сума операції: {money(it["operationAmount"], it["currencyCode"])}')
    if it.get("cashbackAmount"):
        lines.append(f'Кешбек: {money(it["cashbackAmount"], cur)}')
    if "balance" in it:
        lines.append(f'Баланс: {money(it["balance"], cur)}')
    lines.append(f'<i>{html.escape(acc["label"])}</i>')
    if cur == 980 and abs(amt) / 100 >= ALERT_UAH:
        lines.insert(0, "⚠️ <b>Велика операція!</b>")
    return "\n".join(lines)


async def handle_event(payload: dict):
    try:
        if payload.get("type") != "StatementItem":
            return
        d = payload["data"]
        it = d["statementItem"]
        acc = state["accounts"].get(d["account"])
        if acc is None:
            acc = {"label": "рахунок", "balance": 0, "credit": 0,
                   "currency": it.get("currencyCode", 980)}
        if "balance" in it:
            acc["balance"] = it["balance"]
        await bot.send_message(OWNER_ID, fmt_item(it, acc))
    except Exception:
        log.exception("event handling failed")


async def mono_verify(request: web.Request):
    return web.Response(text="ok")


async def mono_hook(request: web.Request):
    payload = await request.json()
    asyncio.create_task(handle_event(payload))
    return web.Response(text="ok")


router = Router()
router.message.filter(F.from_user.id == OWNER_ID)


@router.message(Command("start"))
async def cmd_start(m: Message):
    await m.answer("Привіт! Я надсилатиму сповіщення про всі операції.\n"
                   "/balance — баланси\n/last — останні операції")


@router.message(Command("balance"))
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
async def cmd_last(m: Message):
    parts = (m.text or "").split()
    try:
        await refresh()
        ids = list(state["accounts"])
        if not ids:
            raise RuntimeError("Рахунків не знайдено")
        if len(parts) > 1 and parts[1].isdigit() and 1 <= int(parts[1]) <= len(ids):
            acc_id = ids[int(parts[1]) - 1]
        else:
            acc_id = next((i for i, a in state["accounts"].items() if a["currency"] == 980), ids[0])
        now = int(time.time())
        items = await mono("GET", f"/personal/statement/{acc_id}/{now - 7 * 86400}/{now}")
    except Exception as e:
        return await m.answer(html.escape(str(e)))
    acc = state["accounts"][acc_id]
    head = f'<b>{html.escape(acc["label"])}</b> за 7 днів\n'
    if not items:
        return await m.answer(head + "Операцій немає. Інша картка: /last 2, /last 3 "
                                     "(номери з /balance)")
    out = [f'{"🔴" if i["amount"] < 0 else "🟢"} {money(i["amount"], acc["currency"])} — '
           f'{html.escape(i.get("description", ""))}' for i in items[:10]]
    await m.answer(head + "\n".join(out))


async def set_webhook():
    if not PUBLIC_URL:
        log.warning("PUBLIC_URL не задано — вебхук не встановлено")
        return
    url = f"{PUBLIC_URL}/mono/{SECRET}"
    await mono("POST", "/personal/webhook", json={"webHookUrl": url})
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

    await bot.send_message(OWNER_ID, "✅ Бот запущено")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
