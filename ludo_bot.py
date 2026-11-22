"""
🎲 LUDO — Telegram bot (aiogram 3 + Pillow)
- Shaffof "glass" dizayn (rasm ko'rinishida yangilanadigan taxta)
- Har bir o'yinchining o'z kubik tugmasi
- Animatsiyali haqiqiy Telegram kubigi (🎲)
- Qotib qolmaydi: har yurishga taymer, xatolikdan tiklanish, bloklanmaydigan render
Ishga tushirish:  BOT_TOKEN=xxx python ludo_bot.py
"""
import asyncio
import html
import io
import logging
import math
import os
import random
from dataclasses import dataclass, field
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery,
                           InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, Message)
from PIL import Image, ImageDraw, ImageFilter, ImageFont

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ludo")

TOKEN = os.getenv("BOT_TOKEN", "")
TURN_TIMEOUT = 45          # soniya: o'yinchi yurmasa bot o'zi yuradi
MAX_MISSED = 3             # ketma-ket 3 marta o'tkazib yuborsa o'yindan chiqariladi
LOBBY_TTL = 600

# ───────────────────────── O'YIN QOIDALARI / TAXTA ─────────────────────────
EMO = ["🔴", "🟢", "🟡", "🔵"]
NUM = ["①", "②", "③", "④"]
COLORS = [(235, 64, 82), (46, 204, 113), (255, 196, 40), (52, 152, 255)]
COLOR_ORDER = [0, 2, 1, 3]      # 2 kishi bo'lsa qarama-qarshi burchaklarda
START = [0, 13, 26, 39]
SAFE = {0, 13, 26, 39, 8, 21, 34, 47}
YARD = [(0, 0), (9, 0), (9, 9), (0, 9)]
HOME = [
    [(c, 7) for c in range(1, 6)],
    [(7, r) for r in range(1, 6)],
    [(c, 7) for c in range(13, 8, -1)],
    [(7, r) for r in range(13, 8, -1)],
]


def build_path():
    p = []
    p += [(c, 6) for c in range(1, 6)]
    p += [(6, r) for r in range(5, -1, -1)]
    p += [(7, 0), (8, 0)]
    p += [(8, r) for r in range(1, 6)]
    p += [(c, 6) for c in range(9, 15)]
    p += [(14, 7), (14, 8)]
    p += [(c, 8) for c in range(13, 8, -1)]
    p += [(8, r) for r in range(9, 15)]
    p += [(7, 14), (6, 14)]
    p += [(6, r) for r in range(13, 8, -1)]
    p += [(c, 8) for c in range(5, -1, -1)]
    p += [(0, 7), (0, 6)]
    return p


PATH = build_path()
assert len(PATH) == 52


@dataclass
class Player:
    uid: int
    name: str
    color: int = 0
    bot: bool = False
    tokens: list = field(default_factory=lambda: [-1, -1, -1, -1])  # -1 uy, 0..50 yo'l, 51..55 uy yo'lagi, 56 marra
    missed: int = 0


@dataclass
class Game:
    chat_id: int
    host: int
    players: list
    state: str = "LOBBY"   # LOBBY WAIT_ROLL ROLLING WAIT_MOVE MOVING OVER
    turn: int = 0
    dice: int = 0
    sixes: int = 0
    gid: int = 0
    ver: int = 0
    msg_id: Optional[int] = None
    movable: list = field(default_factory=list)
    last: dict = field(default_factory=dict)
    winner: Optional[Player] = None
    timer: Optional[asyncio.Task] = None
    bot_seq: int = 0

    @property
    def cur(self) -> Player:
        return self.players[self.turn]


GAMES: dict[int, Game] = {}
TASKS: set = set()


def spawn(coro):
    t = asyncio.create_task(coro)
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)
    return t


def esc_name(s: str) -> str:
    return html.escape((s or "Player")[:14])


def tok_cell(color: int, p: int) -> Optional[int]:
    return (START[color] + p) % 52 if 0 <= p <= 50 else None


def movable(p: Player, v: int) -> list:
    res = []
    for i, t in enumerate(p.tokens):
        if t == -1:
            if v == 6:
                res.append(i)
        elif t + v <= 56:
            res.append(i)
    return res


def score_move(g: Game, p: Player, i: int) -> int:
    v, t = g.dice, p.tokens[i]
    new = 0 if t == -1 else t + v
    s = new
    if new == 56:
        s += 100
    elif new <= 50:
        idx = tok_cell(p.color, new)
        if idx in SAFE:
            s += 5
        else:
            for q in g.players:
                if q is p:
                    continue
                if any(tok_cell(q.color, tt) == idx for tt in q.tokens):
                    s += 60
    if t == -1:
        s += 30
    return s


def best_move(g: Game, opts: list) -> int:
    p = g.cur
    return max(opts, key=lambda i: score_move(g, p, i))


# ───────────────────────── RASM (glass dizayn) ─────────────────────────
M, CELL = 30, 56
W = M * 2 + CELL * 15
_bg = None
_fonts: dict = {}


def font(sz: int):
    if sz in _fonts:
        return _fonts[sz]
    f = None
    for p in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "arialbd.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"):
        try:
            f = ImageFont.truetype(p, sz)
            break
        except OSError:
            continue
    if f is None:
        try:
            f = ImageFont.load_default(sz)
        except TypeError:
            f = ImageFont.load_default()
    _fonts[sz] = f
    return f


def star(d, cx, cy, R, fill):
    pts = []
    for i in range(10):
        a = -math.pi / 2 + i * math.pi / 5
        r = R if i % 2 == 0 else R * 0.45
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    d.polygon(pts, fill=fill)


def cell_box(d, c, r, fill, outline, inset=3):
    x, y = M + c * CELL, M + r * CELL
    d.rounded_rectangle([x + inset, y + inset, x + CELL - inset, y + CELL - inset],
                        radius=11, fill=fill, outline=outline, width=2)


def background() -> Image.Image:
    global _bg
    if _bg is not None:
        return _bg.copy()
    im = Image.new("RGBA", (W, W))
    d = ImageDraw.Draw(im)
    top, bot = (20, 14, 52), (8, 44, 76)
    for y in range(W):
        t = y / W
        d.line([(0, y), (W, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)) + (255,))
    blobs = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    bd = ImageDraw.Draw(blobs)
    for cx, cy, r, col in [(150, 150, 220, COLORS[0]), (760, 140, 220, COLORS[1]),
                           (760, 770, 230, COLORS[2]), (140, 760, 230, COLORS[3]),
                           (450, 450, 190, (150, 90, 255))]:
        bd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col + (120,))
    blobs = blobs.filter(ImageFilter.GaussianBlur(90))
    im = Image.alpha_composite(im, blobs).convert("RGB")   # RGB ustiga "RGBA" rejimida chizsak shaffoflik to'g'ri aralashadi
    g = ImageDraw.Draw(im, "RGBA")
    # shisha panel
    g.rounded_rectangle([M - 14, M - 14, W - M + 14, W - M + 14], radius=38,
                        fill=(255, 255, 255, 24), outline=(255, 255, 255, 100), width=2)
    # hovlilar
    for c in range(4):
        ox, oy = YARD[c]
        col = COLORS[c]
        x0, y0 = M + ox * CELL, M + oy * CELL
        g.rounded_rectangle([x0 + 6, y0 + 6, x0 + 6 * CELL - 6, y0 + 6 * CELL - 6], radius=30,
                            fill=col + (78,), outline=col + (200,), width=3)
        g.rounded_rectangle([x0 + CELL, y0 + CELL, x0 + 5 * CELL, y0 + 5 * CELL], radius=26,
                            fill=(255, 255, 255, 34), outline=(255, 255, 255, 70), width=2)
        for dx, dy in [(2, 2), (4, 2), (2, 4), (4, 4)]:
            cx, cy = M + (ox + dx) * CELL, M + (oy + dy) * CELL
            rr = CELL * 0.42
            g.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=(0, 0, 0, 70), outline=col + (160,), width=2)
    # yo'l katakchalari
    for i, (c, r) in enumerate(PATH):
        cell_box(g, c, r, (255, 255, 255, 34), (255, 255, 255, 90))
    for k in range(4):
        for (c, r) in HOME[k]:
            cell_box(g, c, r, COLORS[k] + (120,), COLORS[k] + (230,))
        c, r = PATH[START[k]]
        cell_box(g, c, r, COLORS[k] + (165,), COLORS[k] + (255,))
    for idx in SAFE:
        if idx in START:
            continue
        c, r = PATH[idx]
        star(g, M + (c + .5) * CELL, M + (r + .5) * CELL, CELL * .28, (255, 255, 255, 200))
    for k in START:
        c, r = PATH[k]
        star(g, M + (c + .5) * CELL, M + (r + .5) * CELL, CELL * .22, (255, 255, 255, 235))
    # markaz
    def P(x, y):
        return (M + x * CELL, M + y * CELL)
    mid = P(7.5, 7.5)
    tri = [([P(6, 6), P(6, 9), mid], 0), ([P(6, 6), P(9, 6), mid], 1),
           ([P(9, 6), P(9, 9), mid], 2), ([P(6, 9), P(9, 9), mid], 3)]
    for pts, k in tri:
        g.polygon(pts, fill=COLORS[k] + (175,), outline=(255, 255, 255, 140))
    _bg = im
    return im.copy()


def tok_xy(color: int, ti: int, p: int):
    if p == -1:
        ox, oy = YARD[color]
        dx, dy = [(2, 2), (4, 2), (2, 4), (4, 4)][ti]
        return ox + dx, oy + dy
    if p <= 50:
        c, r = PATH[(START[color] + p) % 52]
        return c + .5, r + .5
    if p <= 55:
        c, r = HOME[color][p - 51]
        return c + .5, r + .5
    ox, oy = [(-.8, 0), (0, -.8), (.8, 0), (0, .8)][color]
    return 7.5 + ox, 7.5 + oy


def draw_token(d, x, y, r, col, label, hl):
    for k, a in ((1.9, 20), (1.5, 36), (1.2, 60)):
        d.ellipse([x - r * k, y - r * k, x + r * k, y + r * k], fill=col + (a,))
    if hl:
        d.ellipse([x - r - 9, y - r - 9, x + r + 9, y + r + 9], outline=(255, 255, 255, 255), width=4)
    d.ellipse([x - r, y - r, x + r, y + r], fill=col + (255,), outline=(255, 255, 255, 240), width=3)
    d.ellipse([x - r * .7, y - r * .8, x + r * .1, y - r * .1], fill=(255, 255, 255, 95))
    d.text((x, y + 1), label, font=font(int(r * 1.05)), fill=(20, 20, 45, 255), anchor="mm")


OFFS = [(-.2, -.2), (.2, -.2), (-.2, .2), (.2, .2)]


def render_board(snap: list, hl: set) -> bytes:
    im = background()
    d = ImageDraw.Draw(im, "RGBA")
    groups: dict = {}
    for color, tokens in snap:
        for ti, p in enumerate(tokens):
            x, y = tok_xy(color, ti, p)
            groups.setdefault((round(x, 2), round(y, 2)), []).append((color, ti))
    for (x, y), items in groups.items():
        n = len(items)
        for k, (color, ti) in enumerate(items):
            if n > 1:
                dx, dy = OFFS[k % 4]
                r = CELL * .25
            else:
                dx = dy = 0
                r = CELL * .36
            draw_token(d, M + (x + dx) * CELL, M + (y + dy) * CELL, r, COLORS[color], str(ti + 1),
                       (color, ti) in hl)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


# ───────────────────────── UI (matn + tugmalar) ─────────────────────────
def lobby_text(g: Game) -> str:
    lines = ["🎲 <b>LUDO</b> — yangi o'yin", "", f"Ishtirokchilar ({len(g.players)}/4):"]
    for i, p in enumerate(g.players):
        crown = " 👑" if p.uid == g.host else ""
        lines.append(f"{EMO[COLOR_ORDER[i]]} {html.escape(p.name) if p.bot else p.name}{crown}")
    lines += ["", "Kamida 2 kishi kerak. Do'stlaringiz «Qo'shilish»ni bossin!"]
    return "\n".join(lines)


def lobby_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Qo'shilish", callback_data="L:j"),
         InlineKeyboardButton(text="🤖 Bot qo'shish", callback_data="L:b")],
        [InlineKeyboardButton(text="▶️ Boshlash", callback_data="L:s"),
         InlineKeyboardButton(text="❌ Bekor", callback_data="L:x")],
    ])


def game_caption(g: Game, note: str = "") -> str:
    lines = ["🎲 <b>LUDO</b>", ""]
    for i, p in enumerate(g.players):
        done = sum(1 for t in p.tokens if t == 56)
        out = sum(1 for t in p.tokens if 0 <= t < 56)
        mark = "👉" if (i == g.turn and g.state != "OVER") else "▫️"
        lines.append(f"{mark} {EMO[p.color]} <b>{p.name}</b> — 🏠 {done}/4 · 🏃 {out}")
    if g.state == "OVER":
        lines.append("")
        lines.append(f"🏆 <b>G'olib: {EMO[g.winner.color]} {g.winner.name}</b>" if g.winner
                     else "⏹ O'yin to'xtatildi")
        if note:
            lines.append(note)
        lines.append("Yangi o'yin: /ludo")
    else:
        if note:
            lines += ["", note]
        if g.state == "WAIT_ROLL":
            lines += ["", f"⏱ {TURN_TIMEOUT}s ichida o'z tugmangizni bosing"]
    return "\n".join(lines)


def game_kb(g: Game) -> InlineKeyboardMarkup:
    rows = []
    if g.state == "OVER":
        return InlineKeyboardMarkup(inline_keyboard=[])
    if g.state in ("WAIT_MOVE", "MOVING"):
        rows.append([InlineKeyboardButton(text=f"{NUM[i]} dona", callback_data=f"m:{g.gid}:{i}")
                     for i in g.movable])
    else:
        for i, p in enumerate(g.players):
            last = g.last.get(p.uid)
            dice = f"🎲{last}" if last else "🎲"
            if i == g.turn:
                text = f"👉 {EMO[p.color]} {p.name} — TASHLASH {dice}"
            else:
                text = f"{EMO[p.color]} {p.name} {dice}"
            rows.append([InlineKeyboardButton(text=text, callback_data=f"r:{g.gid}:{p.uid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def ans(cb: CallbackQuery, text: Optional[str] = None, alert: bool = False):
    try:
        await cb.answer(text, show_alert=alert)
    except Exception:
        pass


async def push_board(g: Game, bot: Bot, note: str = ""):
    snap = [(p.color, list(p.tokens)) for p in g.players]
    hl = set()
    if g.state in ("WAIT_MOVE", "MOVING") and g.players:
        hl = {(g.cur.color, i) for i in g.movable}
    img = await asyncio.to_thread(render_board, snap, hl)   # event loop bloklanmaydi
    cap, kb = game_caption(g, note), game_kb(g)
    file = BufferedInputFile(img, "board.jpg")

    async def send_new():
        old = g.msg_id
        m = await bot.send_photo(g.chat_id, file, caption=cap, reply_markup=kb)
        g.msg_id = m.message_id
        if old:
            try:
                await bot.delete_message(g.chat_id, old)
            except Exception:
                pass

    if g.msg_id is None:
        await send_new()
        return
    for attempt in range(2):
        try:
            await bot.edit_message_media(
                media=InputMediaPhoto(media=BufferedInputFile(img, "board.jpg"), caption=cap,
                                      parse_mode=ParseMode.HTML),
                chat_id=g.chat_id, message_id=g.msg_id, reply_markup=kb)
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(min(e.retry_after, 10) + 0.5)
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
            await send_new()
            return
    await send_new()


# ───────────────────────── O'YIN OQIMI ─────────────────────────
def stop_timer(g: Game):
    if g.timer and g.timer is not asyncio.current_task():
        g.timer.cancel()
    g.timer = None


def start_timer(g: Game, bot: Bot):
    stop_timer(g)
    g.timer = asyncio.create_task(turn_timer(g, bot, g.ver))


async def guarded(g: Game, bot: Bot, coro):
    """Xatolik bo'lsa o'yin qotib qolmasligi uchun tiklaydi."""
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("O'yin xatosi")
        if GAMES.get(g.chat_id) is g and g.state not in ("OVER", "LOBBY"):
            try:
                await begin_wait(g, bot, "⚠️ Kichik xatolik tiklandi, davom eting")
            except Exception:
                log.exception("Tiklash bajarilmadi")


async def begin_wait(g: Game, bot: Bot, note: str = ""):
    g.state = "WAIT_ROLL"
    g.ver += 1
    g.movable = []
    await push_board(g, bot, note)
    if g.cur.bot:
        spawn(guarded(g, bot, bot_roll(g, bot, g.ver)))
    else:
        start_timer(g, bot)


async def advance(g: Game, bot: Bot, note: str = ""):
    g.turn = (g.turn + 1) % len(g.players)
    g.sixes = 0
    await begin_wait(g, bot, note)


async def bot_roll(g: Game, bot: Bot, ver: int):
    await asyncio.sleep(1.2)
    if GAMES.get(g.chat_id) is g and g.ver == ver and g.state == "WAIT_ROLL":
        await do_roll(g, bot)


async def delete_later(bot: Bot, msg: Message, delay: float):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


async def do_roll(g: Game, bot: Bot):
    stop_timer(g)
    g.state = "ROLLING"
    p = g.cur
    val, dmsg = None, None
    try:
        dmsg = await bot.send_dice(g.chat_id, emoji="🎲")
        val = dmsg.dice.value
        await asyncio.sleep(3.3)          # kubik animatsiyasi tugashini kutamiz
    except Exception:
        log.warning("send_dice ishlamadi, oddiy random ishlatildi")
    if val is None:
        val = random.randint(1, 6)
    if dmsg:
        spawn(delete_later(bot, dmsg, 5))
    if GAMES.get(g.chat_id) is not g or g.state != "ROLLING":
        return
    g.dice = val
    g.last[p.uid] = val
    g.sixes = g.sixes + 1 if val == 6 else 0
    name = f"{EMO[p.color]} <b>{p.name}</b>"
    if g.sixes >= 3:
        await advance(g, bot, f"{name}: 🎲 6 uch marta — navbat o'tdi ❌")
        return
    seen: dict = {}
    for i in movable(p, val):
        seen.setdefault(p.tokens[i], i)    # bir xil joydagi donalar bitta variant
    opts = list(seen.values())
    if not opts:
        note = f"{name}: 🎲 {val} — yurish imkoni yo'q"
        if val == 6:
            await begin_wait(g, bot, note)
        else:
            await advance(g, bot, note)
        return
    if len(opts) == 1 or p.bot:
        await apply_move(g, bot, opts[0] if len(opts) == 1 else best_move(g, opts))
        return
    g.movable = opts
    g.state = "WAIT_MOVE"
    g.ver += 1
    await push_board(g, bot, f"{name}: 🎲 {val} — donani tanlang 👇")
    start_timer(g, bot)


async def apply_move(g: Game, bot: Bot, ti: int):
    stop_timer(g)
    p, v = g.cur, g.dice
    g.state = "MOVING"
    old = p.tokens[ti]
    new = 0 if old == -1 else old + v
    p.tokens[ti] = new
    extra = v == 6
    name = f"{EMO[p.color]} <b>{p.name}</b>"
    note = f"{name}: 🎲 {v} → {NUM[ti]}-dona yurdi"
    if old == -1:
        note = f"{name}: 🎲 6 → {NUM[ti]}-dona maydonga chiqdi 🚀"
    if new == 56:
        note += "\n🏠 Dona manzilga yetdi! Qo'shimcha yurish"
        extra = True
    elif new <= 50:
        idx = tok_cell(p.color, new)
        if idx not in SAFE:
            hit = []
            for q in g.players:
                if q is p:
                    continue
                for j, t in enumerate(q.tokens):
                    if tok_cell(q.color, t) == idx:
                        q.tokens[j] = -1
                        hit.append(q)
            if hit:
                names = ", ".join(sorted({f"{EMO[q.color]} {q.name}" for q in hit}))
                note += f"\n💥 {names} donasi urildi! Qo'shimcha yurish"
                extra = True
    if all(t == 56 for t in p.tokens):
        g.state = "OVER"
        g.winner = p
        GAMES.pop(g.chat_id, None)
        await push_board(g, bot, note)
        return
    if extra:
        await begin_wait(g, bot, note)
    else:
        await advance(g, bot, note)


async def finish(g: Game, bot: Bot, winner: Optional[Player], note: str = ""):
    stop_timer(g)
    g.state = "OVER"
    g.winner = winner
    GAMES.pop(g.chat_id, None)
    if g.players:
        await push_board(g, bot, note)


async def drop_current(g: Game, bot: Bot):
    p = g.cur
    g.players.pop(g.turn)
    note = f"🚪 {p.name} faol emas — o'yindan chiqarildi"
    humans = [x for x in g.players if not x.bot]
    if len(g.players) < 2 or not humans:
        await finish(g, bot, g.players[0] if (len(g.players) == 1 and humans) else None, note)
        return
    g.turn %= len(g.players)
    g.sixes = 0
    await begin_wait(g, bot, note)


async def turn_timer(g: Game, bot: Bot, ver: int):
    await asyncio.sleep(TURN_TIMEOUT)
    if GAMES.get(g.chat_id) is not g or g.ver != ver or g.state not in ("WAIT_ROLL", "WAIT_MOVE"):
        return
    p = g.cur
    p.missed += 1
    if p.missed >= MAX_MISSED:
        await guarded(g, bot, drop_current(g, bot))
    elif g.state == "WAIT_ROLL":
        await guarded(g, bot, do_roll(g, bot))
    else:
        await guarded(g, bot, apply_move(g, bot, best_move(g, g.movable)))


# ───────────────────────── HANDLERLAR ─────────────────────────
router = Router()

HELP = (
    "🎲 <b>LUDO bot</b>\n\n"
    "Guruhga qo'shing va <b>/ludo</b> yuboring.\n"
    "• 2–4 o'yinchi (xohlasangiz 🤖 bot ham qo'shiladi)\n"
    "• Har bir o'yinchining <b>o'z kubik tugmasi</b> bor\n"
    "• 6 tushsa dona chiqadi va yana yurasiz\n"
    "• Raqib donasini ursangiz yana yurasiz (⭐ va start kataklarda xavfsiz)\n"
    "• Birinchi bo'lib 4 ta donani markazga olib kirgan yutadi\n"
    f"• {TURN_TIMEOUT}s yurmasangiz bot o'zi yuradi\n\n"
    "/ludo — yangi o'yin\n/stop — o'yinni to'xtatish (host yoki admin)"
)


@router.message(CommandStart())
async def cmd_start(m: Message):
    await m.answer(HELP)


@router.message(Command("help", "rules"))
async def cmd_help(m: Message):
    await m.answer(HELP)


@router.message(Command("ludo", "play"))
async def cmd_ludo(m: Message, bot: Bot):
    if m.chat.id in GAMES:
        await m.answer("Bu chatda o'yin davom etmoqda. To'xtatish: /stop")
        return
    g = Game(chat_id=m.chat.id, host=m.from_user.id,
             players=[Player(m.from_user.id, esc_name(m.from_user.full_name))])
    GAMES[m.chat.id] = g
    sent = await m.answer(lobby_text(g), reply_markup=lobby_kb())
    g.msg_id = sent.message_id
    spawn(lobby_expire(g, bot))


async def lobby_expire(g: Game, bot: Bot):
    await asyncio.sleep(LOBBY_TTL)
    if GAMES.get(g.chat_id) is g and g.state == "LOBBY":
        GAMES.pop(g.chat_id, None)
        try:
            await bot.delete_message(g.chat_id, g.msg_id)
        except Exception:
            pass


@router.message(Command("stop"))
async def cmd_stop(m: Message, bot: Bot):
    g = GAMES.get(m.chat.id)
    if not g:
        await m.answer("Hozir o'yin yo'q. Boshlash: /ludo")
        return
    ok = m.from_user.id == g.host
    if not ok:
        try:
            member = await bot.get_chat_member(m.chat.id, m.from_user.id)
            ok = member.status in ("administrator", "creator")
        except Exception:
            pass
    if not ok:
        await m.answer("Faqat host yoki admin to'xtata oladi.")
        return
    if g.state == "LOBBY":
        GAMES.pop(m.chat.id, None)
        try:
            await bot.delete_message(g.chat_id, g.msg_id)
        except Exception:
            pass
        await m.answer("❌ O'yin bekor qilindi.")
        return
    await guarded(g, bot, finish(g, bot, None, "Host to'xtatdi"))


@router.callback_query(F.data.startswith("L:"))
async def cb_lobby(cb: CallbackQuery, bot: Bot):
    g = GAMES.get(cb.message.chat.id)
    if not g or g.state != "LOBBY" or g.msg_id != cb.message.message_id:
        return await ans(cb, "Bu lobby eskirgan", True)
    act, u = cb.data[2:], cb.from_user
    if act == "j":
        if any(p.uid == u.id for p in g.players):
            return await ans(cb, "Siz allaqachon qo'shilgansiz ✅")
        if len(g.players) >= 4:
            return await ans(cb, "Joy qolmadi (4/4)", True)
        g.players.append(Player(u.id, esc_name(u.full_name)))
    elif act == "b":
        if u.id != g.host:
            return await ans(cb, "Bot qo'shish faqat host uchun", True)
        if len(g.players) >= 4:
            return await ans(cb, "Joy qolmadi (4/4)", True)
        g.bot_seq += 1
        g.players.append(Player(-g.bot_seq, f"🤖 Bot{g.bot_seq}", bot=True))
    elif act == "x":
        if u.id != g.host:
            return await ans(cb, "Faqat host bekor qila oladi", True)
        GAMES.pop(g.chat_id, None)
        await ans(cb)
        try:
            await cb.message.edit_text("❌ O'yin bekor qilindi.")
        except Exception:
            pass
        return
    elif act == "s":
        if u.id != g.host:
            return await ans(cb, "Faqat host boshlay oladi 👑", True)
        if len(g.players) < 2:
            return await ans(cb, "Kamida 2 o'yinchi kerak (🤖 Bot qo'shishingiz mumkin)", True)
        await ans(cb)
        for i, p in enumerate(g.players):
            p.color = COLOR_ORDER[i]
        g.players.sort(key=lambda p: p.color)
        g.gid = random.randint(1000, 9999)
        g.turn, g.msg_id = 0, None
        try:
            await cb.message.delete()
        except Exception:
            pass
        await guarded(g, bot, begin_wait(g, bot, "🚀 O'yin boshlandi! Omad!"))
        return
    await ans(cb)
    try:
        await cb.message.edit_text(lobby_text(g), reply_markup=lobby_kb())
    except TelegramBadRequest:
        pass


def _game_for(cb: CallbackQuery) -> Optional[Game]:
    g = GAMES.get(cb.message.chat.id)
    parts = cb.data.split(":")
    if not g or g.state in ("LOBBY", "OVER") or str(g.gid) != parts[1]:
        return None
    return g


@router.callback_query(F.data.startswith("r:"))
async def cb_roll(cb: CallbackQuery, bot: Bot):
    g = _game_for(cb)
    if not g:
        return await ans(cb, "Bu o'yin tugagan", True)
    owner = int(cb.data.split(":")[2])
    u = cb.from_user.id
    if u != owner:
        if any(p.uid == u for p in g.players):
            return await ans(cb, "Bu sizning kubik tugmangiz emas 🙂", True)
        return await ans(cb, "Siz bu o'yinda emassiz", True)
    cur = g.cur
    if cur.uid != u:
        return await ans(cb, f"Hozir {cur.name}ning navbati ⏳", True)
    if g.state != "WAIT_ROLL":
        return await ans(cb, "Kubik aylanmoqda…" if g.state == "ROLLING" else "Avval donani tanlang 👆")
    g.state = "ROLLING"            # darhol — ikki marta bosishdan himoya
    cur.missed = 0
    await ans(cb, "🎲")
    spawn(guarded(g, bot, do_roll(g, bot)))


@router.callback_query(F.data.startswith("m:"))
async def cb_move(cb: CallbackQuery, bot: Bot):
    g = _game_for(cb)
    if not g:
        return await ans(cb, "Bu o'yin tugagan", True)
    if g.cur.uid != cb.from_user.id:
        return await ans(cb, f"Hozir {g.cur.name}ning navbati ⏳", True)
    ti = int(cb.data.split(":")[2])
    if g.state != "WAIT_MOVE" or ti not in g.movable:
        return await ans(cb, "Bu yurish mumkin emas")
    g.state = "MOVING"
    g.cur.missed = 0
    await ans(cb)
    spawn(guarded(g, bot, apply_move(g, bot, ti)))


async def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN o'zgaruvchisini o'rnating: export BOT_TOKEN=123:ABC")
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Ludo bot ishga tushdi")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
