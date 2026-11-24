import io
import os
from datetime import datetime

import matplotlib

matplotlib.use("Agg")
from matplotlib.figure import Figure  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

CUR = {980: "₴", 840: "$", 978: "€"}

BG = (14, 14, 18)
CARD = (27, 27, 34)
LINE = (48, 48, 58)
GRAY = (140, 140, 155)
WHITE = (235, 235, 240)


def money(kop, code) -> str:
    return f"{kop / 100:,.2f}".replace(",", " ") + f" {CUR.get(code, code)}"


def clean(s) -> str:
    """Прибирає емодзі, яких немає в шрифті."""
    return "".join(
        ch for ch in str(s)
        if ord(ch) <= 0xFFFF and not (0x2600 <= ord(ch) <= 0x27BF) and ord(ch) != 0xFE0F
    )


def _fonts():
    d = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
    reg = os.path.join(d, "DejaVuSans.ttf")
    bold = os.path.join(d, "DejaVuSans-Bold.ttf")
    return (lambda s: ImageFont.truetype(reg, s)), (lambda s: ImageFont.truetype(bold, s))


def _fit(d, text, font, maxw):
    if d.textlength(text, font=font) <= maxw:
        return text
    while text and d.textlength(text + "…", font=font) > maxw:
        text = text[:-1]
    return text + "…"


def make_receipt(r: dict, acc_label: str, cat_name: str, tz) -> bytes:
    reg, bold = _fonts()
    cur = r["cur"]
    when = datetime.fromtimestamp(r["time"], tz).strftime("%d.%m.%Y %H:%M:%S")

    rows = [
        ("Дата", when),
        ("Категорія", cat_name),
        ("Рахунок", acc_label),
    ]
    if r["op_cur"] != cur:
        rows.append(("Сума операції", money(r["op_amount"], r["op_cur"])))
    if r.get("commission"):
        rows.append(("Комісія", money(r["commission"], cur)))
    if r.get("cashback"):
        rows.append(("Кешбек", money(r["cashback"], cur)))
    if r.get("counter"):
        rows.append(("Контрагент", r["counter"]))
    if r.get("comment"):
        rows.append(("Коментар", r["comment"]))
    if r.get("balance") is not None:
        rows.append(("Баланс після", money(r["balance"], cur)))
    if r.get("receipt"):
        rows.append(("№ квитанції", r["receipt"]))
    rows.append(("ID операції", r["id"]))

    W, PAD, ROW, TOP = 900, 70, 74, 330
    H = TOP + ROW * len(rows) + 150
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((30, 30, W - 30, H - 30), radius=40, fill=CARD)

    d.text((PAD, 80), "КВИТАНЦІЯ", font=bold(30), fill=GRAY)
    color = (255, 99, 99) if r["amount"] < 0 else (80, 220, 140)
    d.text((PAD, 140), money(r["amount"], cur), font=bold(80), fill=color)
    f_desc = reg(32)
    d.text((PAD, 255), _fit(d, clean(r["description"]), f_desc, W - 2 * PAD),
           font=f_desc, fill=WHITE)

    f_label, f_val = reg(28), reg(28)
    y = TOP
    for label, val in rows:
        d.line((PAD, y, W - PAD, y), fill=LINE, width=2)
        d.text((PAD, y + 22), label, font=f_label, fill=GRAY)
        lw = d.textlength(label, font=f_label)
        val = _fit(d, clean(val), f_val, W - 2 * PAD - lw - 30)
        vw = d.textlength(val, font=f_val)
        d.text((W - PAD - vw, y + 22), val, font=f_val, fill=WHITE)
        y += ROW

    d.text((PAD, H - 105), "Створено ботом · не є офіційним документом банку",
           font=reg(22), fill=(110, 110, 125))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_chart(title: str, items: list) -> bytes:
    """items: [(назва, сума_у_гривнях)] від більшого до меншого."""
    items = items[:10]
    names = [clean(n) for n, _ in items][::-1]
    vals = [v for _, v in items][::-1]
    fig = Figure(figsize=(7, 0.6 * len(items) + 1.7), dpi=160, facecolor="#17171c")
    ax = fig.add_subplot(111, facecolor="#17171c")
    ax.barh(names, vals, color="#5b8cff", height=0.6)
    top = max(vals) if vals else 1
    for i, v in enumerate(vals):
        ax.text(v + top * 0.02, i, f"{v:,.0f} ₴".replace(",", " "),
                va="center", color="white", fontsize=10)
    ax.set_xlim(0, top * 1.28)
    ax.set_title(clean(title), color="white", fontsize=13, loc="left", pad=12)
    ax.tick_params(colors="white", labelsize=11, length=0)
    ax.get_xaxis().set_visible(False)
    for s in ax.spines.values():
        s.set_visible(False)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    return buf.getvalue()
