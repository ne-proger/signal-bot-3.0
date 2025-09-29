# src/main.py
from __future__ import annotations
from .analysis import analyze_and_decide
import asyncio
import logging
import os
from typing import Optional

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    CallbackQueryHandler, MessageHandler, TypeHandler, filters
)
from telegram.error import Conflict

from src.config import Settings
from src.storage import Storage
from src.utils import (
    parse_frequency, norm_pairs, validate_sensitivity, ParseError,
    validate_category, CONF_THRESHOLDS
)
from src.scheduler import BotScheduler

try:
    from .bybit_client import BybitClient, BybitError  # type: ignore
except Exception:
    class BybitError(Exception): ...
    class BybitClient:  # type: ignore
        def __init__(self, proxy_url: str | None = None): self.proxy_url = proxy_url
        def latest_ohlcv_pack(self, symbol: str, category: str = "spot"):
            return {"W": [], "D": [], "240": []}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("bot")

# --- globals ---
settings: Optional[Settings] = None
store: Optional[Storage] = None
scheduler: Optional[BotScheduler] = None
bybit: Optional[BybitClient] = None  # type: ignore

WELCOME = (
    "Привет! Я крипто-бот.\n"
    "Команды:\n"
    "/settings — меню настроек (кнопки)\n"
    "/debugbtn — тест кнопок\n"
    "/setpairs BTCUSDT,TRXUSDT — задать пары\n"
    "/setfreq 5m|1h|1d — периодичность\n"
    "/setsens low|medium|high — чувствительность\n"
    "/setcat spot|linear — категория рынка\n"
    "/status — показать настройки\n"
    "/testonce — запустить проверку сейчас\n"
)

FREQ_PRESETS = [("1m","60"),("5m","300"),("15m","900"),("1h","3600"),("4h","14400"),("1d","86400")]

# ---------- helpers ----------
async def ensure_user_row(user_id: int):
    assert store is not None and scheduler is not None
    if store.get_user(user_id) is None:
        store.upsert_user(user_id)
        await scheduler.upsert_user_job(user_id, 3600, check_job)

def _build_settings_keyboard(current: dict) -> InlineKeyboardMarkup:
    sens = current.get("sensitivity","medium")
    cat = current.get("category","spot")
    freq_row = [InlineKeyboardButton(txt, callback_data=f"freq:{sec}") for (txt,sec) in FREQ_PRESETS]
    sens_row = [
        InlineKeyboardButton(("• " if sens=="low" else "")+"low", callback_data="sens:low"),
        InlineKeyboardButton(("• " if sens=="medium" else "")+"medium", callback_data="sens:medium"),
        InlineKeyboardButton(("• " if sens=="high" else "")+"high", callback_data="sens:high"),
    ]
    cat_row = [
        InlineKeyboardButton(("• " if cat=="spot" else "")+"spot", callback_data="cat:spot"),
        InlineKeyboardButton(("• " if cat=="linear" else "")+"linear", callback_data="cat:linear"),
    ]
    pairs_row = [InlineKeyboardButton("✏️ Изменить пары", callback_data="pairs:edit")]
    return InlineKeyboardMarkup([freq_row, sens_row, cat_row, pairs_row])

async def _send_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Всегда отправляем НОВОЕ сообщение с меню."""
    assert store is not None and scheduler is not None
    user_id = update.effective_user.id  # type: ignore[union-attr]
    if store.get_user(user_id) is None:
        await ensure_user_row(user_id)
    row = store.get_user(user_id)
    assert row is not None
    _, pairs, freq, sens, category = row
    kb = _build_settings_keyboard({"sensitivity": sens, "category": category})
    text = (
        "⚙️ Настройки бота\n"
        f"Пары: {pairs}\n"
        f"Периодичность: {freq}s\n"
        f"Чувствительность: {sens}\n"
        f"Категория: {category}\n\n"
        "Выберите параметр кнопками ниже:"
    )
    await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=kb)

# ---------- commands ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user and update.message
    await ensure_user_row(update.effective_user.id)
    assert store is not None
    row = store.get_user(update.effective_user.id)
    if row:
        _, pairs, freq, sens, category = row
        await update.message.reply_text(
            WELCOME + f"\nТекущие настройки:\nПары: {pairs}\nПериодичность: {freq}s\nЧувствительность: {sens}\nКатегория: {category}"
        )
    else:
        await update.message.reply_text(WELCOME)

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user and update.message and store is not None
    row = store.get_user(update.effective_user.id)
    if not row:
        await update.message.reply_text("Профиль не найден. Наберите /start")
        return
    _, pairs, freq, sens, category = row
    await update.message.reply_text(
        f"Пары: {pairs}\nПериодичность: {freq}s\nЧувствительность: {sens}\nКатегория: {category}"
    )

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user
    await ensure_user_row(update.effective_user.id)
    await _send_settings_menu(update, context)

async def debugbtn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔧 PING", callback_data="dbg:ping")]])
    await update.message.reply_text("Нажми кнопку — должен прийти callback.", reply_markup=kb)

async def setpairs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user and update.message
    await ensure_user_row(update.effective_user.id)
    if not context.args:
        await update.message.reply_text("Укажите пары через запятую, например: /setpairs BTCUSDT,TRXUSDT")
        return
    assert store is not None
    pairs = norm_pairs(" ".join(context.args))
    store.upsert_user(update.effective_user.id, pairs=pairs)
    await update.message.reply_text(f"Ок. Пары: {pairs}")

async def setfreq_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user and update.message
    await ensure_user_row(update.effective_user.id)
    if not context.args:
        await update.message.reply_text("Укажите период, напр.: /setfreq 5m или /setfreq 1h")
        return
    try:
        seconds = parse_frequency(context.args[0])
    except ParseError as e:
        await update.message.reply_text(f"Ошибка: {e}")
        return
    assert store is not None and scheduler is not None
    store.upsert_user(update.effective_user.id, frequency_seconds=seconds)
    await scheduler.upsert_user_job(update.effective_user.id, seconds, check_job)
    await update.message.reply_text(f"Ок. Периодичность: {seconds} сек")

async def setsens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user and update.message
    await ensure_user_row(update.effective_user.id)
    if not context.args:
        await update.message.reply_text("Укажите чувствительность: /setsens low|medium|high")
        return
    try:
        val = validate_sensitivity(context.args[0])
    except ParseError as e:
        await update.message.reply_text(f"Ошибка: {e}")
        return
    assert store is not None
    store.upsert_user(update.effective_user.id, sensitivity=val)
    await update.message.reply_text(f"Ок. Чувствительность: {val}")

async def setcat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user and update.message
    await ensure_user_row(update.effective_user.id)
    if not context.args:
        await update.message.reply_text("Укажите категорию: /setcat spot|linear")
        return
    try:
        cat = validate_category(context.args[0])
    except ParseError as e:
        await update.message.reply_text(f"Ошибка: {e}")
        return
    assert store is not None
    store.upsert_user(update.effective_user.id, category=cat)
    await update.message.reply_text(f"Ок. Категория: {cat}")

async def testonce_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user and update.message
    await run_check_for_user(update.effective_user.id, context)
    await update.message.reply_text("Тестовая проверка выполнена.")

# ---------- callbacks ----------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.callback_query and update.effective_user
    q = update.callback_query
    data = (q.data or "").strip()
    # тост в Telegram + лог
    try:
        await q.answer(text=f"callback: {data}", show_alert=False)
    except Exception:
        pass
    log.info("callback: %s", data)

    assert store is not None and scheduler is not None
    try:
        if data.startswith("freq:"):
            seconds = int(data.split(":", 1)[1])
            store.upsert_user(update.effective_user.id, frequency_seconds=seconds)
            await scheduler.upsert_user_job(update.effective_user.id, seconds, check_job)
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"⏱ Периодичность: {seconds} сек")
        elif data.startswith("sens:"):
            val = validate_sensitivity(data.split(":", 1)[1])
            store.upsert_user(update.effective_user.id, sensitivity=val)
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"🎚 Чувствительность: {val}")
        elif data.startswith("cat:"):
            cat = validate_category(data.split(":", 1)[1])
            store.upsert_user(update.effective_user.id, category=cat)
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"🪙 Категория: {cat}")
        elif data == "dbg:ping":
            await context.bot.send_message(chat_id=update.effective_chat.id, text="🏓 pong")
        elif data == "pairs:edit":
            context.user_data["await_pairs"] = True
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Введите пары через запятую, например: BTCUSDT,TRXUSDT,INJUSDT",
                reply_markup=ReplyKeyboardRemove()
            )
        else:
            await context.bot.send_message(chat_id=update.effective_chat.id, text="Неизвестная команда кнопки.")
    except ParseError as e:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Ошибка: {e}")

    # Отправим актуальное меню
    await _send_settings_menu(update, context)

# ---------- ввод пар после pairs:edit ----------
async def on_pairs_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("await_pairs"):
        return
    assert update.message and update.effective_user and store is not None
    pairs = norm_pairs(update.message.text or "")
    store.upsert_user(update.effective_user.id, pairs=pairs)
    context.user_data["await_pairs"] = False
    await update.message.reply_text(f"✅ Пары обновлены: {pairs}", reply_markup=ReplyKeyboardRemove())
    await _send_settings_menu(update, context)

# ---------- джобы / проверка ----------
async def check_job(context: ContextTypes.DEFAULT_TYPE):
    user_id = (context.job.data or {}).get("user_id") if context.job else None
    if user_id:
        await run_check_for_user(user_id, context)

async def run_check_for_user(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    assert store is not None and settings is not None and bybit is not None
    row = store.get_user(user_id)
    if not row:
        return
    _, pairs, freq, sens, category = row
    channel_id = settings.telegram_channel_id
    conf_threshold = CONF_THRESHOLDS.get(sens, 0.6)

    lines = [
        "📊 Обновление: запускается анализ.",
        f"Пары: {pairs}",
        f"Периодичность: {freq}s",
        f"Чувствительность: {sens} (порог публикации {conf_threshold:.2f})",
        f"Категория: {category}",
        "",
    ]

    for sym in pairs.split(","):
        sym = sym.strip().upper()
        if not sym:
            continue
        try:
            pack = bybit.latest_ohlcv_pack(sym, category=category)  # type: ignore[attr-defined]
        except Exception as e:
            lines.append(f"• {sym}: ошибка получения данных: {e}")
            continue

        preview = []
        tf_errors = {}
        for tf, label in (("W","W"),("D","D"),("240","4H")):
            bars = pack.get(tf)
            if isinstance(bars, dict) and bars.get("error"):
                tf_errors[tf] = bars["error"]
                preview.append(f"{label}: error")
            else:
                last = (bars[-1] if bars else None)
                preview.append(f"{label}: close={last.get('close') if last else 'n/a'}")
        lines.append(f"• {sym}: "+"; ".join(preview))

        if tf_errors:
            await context.bot.send_message(chat_id=user_id, text=f"[DATA ERROR] {sym}: {tf_errors}")
            continue

        try:
            res = analyze_and_decide(
                symbol=sym,
                ohlcv_pack=pack,
                ma_window=21,
                macd_cfg={"fast":12,"slow":26,"signal":9},
                sensitivity=sens,
                model=settings.openai_model,
                book_url=settings.literature_urls,
            )
        except Exception as e:
            await context.bot.send_message(chat_id=user_id, text=f"Ошибка анализа {sym}: {e}")
            continue

        buy = bool(res.get("buy_signal"))
        rationale = res.get("rationale", "")
        if buy:
            conf = float(res.get("confidence", 0.0) or 0.0)
            entry = res.get("entry"); tp = res.get("take_profit"); sl = res.get("stop_loss"); horizon = res.get("exit_horizon")
            if conf >= conf_threshold:
                msg = (
                    f"🔔 SIGNAL BUY — {sym}\n"
                    f"entry: {entry}\n"
                    f"take_profit: {tp}\n"
                    f"stop_loss: {sl}\n"
                    f"exit_horizon: {horizon}\n"
                    f"confidence: {conf:.2f}\n"
                    f"rationale: {rationale}"
                )
                if channel_id:
                    try: await context.bot.send_message(chat_id=channel_id, text=msg)
                    except Exception as e: log.error("Не удалось отправить сигнал в канал: %s", e)
                await context.bot.send_message(chat_id=user_id, text=f"[SIGNAL] {sym}: buy (confidence {conf:.2f}) — опубликовано.")
            else:
                await context.bot.send_message(chat_id=user_id, text=f"[FILTERED] {sym}: buy confidence {conf:.2f} ниже порога {conf_threshold:.2f} для {sens}")
        else:
            await context.bot.send_message(chat_id=user_id, text=f"[NO SIGNAL] {sym}: {rationale}")

    if lines:
        try: await context.bot.send_message(chat_id=user_id, text="\n".join(lines))
        except Exception as e: log.error("Не удалось отправить сводку пользователю %s: %s", user_id, e)

# ---------- error & debug ----------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Exception in handler", exc_info=context.error)

async def debug_update_logger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # логируем типы апдейтов на всякий случай
    try:
        t = None
        if update.message: t = "message"
        elif update.callback_query: t = "callback_query"
        elif update.edited_message: t = "edited_message"
        elif update.channel_post: t = "channel_post"
        else: t = "other"
        log.info("update type: %s", t)
    except Exception:
        pass

def main():
    global settings, store, scheduler, bybit
    load_dotenv()
    settings = Settings.load()

    token = os.getenv("TELEGRAM_BOT_TOKEN") or (settings.telegram_bot_token if settings else None)
    if not token:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")

    application = ApplicationBuilder().token(token).concurrent_updates(True).build()
    application.add_error_handler(error_handler)

    store = Storage()
    scheduler = BotScheduler(application)

    # --- handlers order matters ---
    application.add_handler(CallbackQueryHandler(on_callback, pattern=".*"), group=0)  # ловим все колбэки
    application.add_handler(TypeHandler(Update, debug_update_logger), group=1)  # диагностический лог

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("settings", settings_cmd))
    application.add_handler(CommandHandler("debugbtn", debugbtn_cmd))
    application.add_handler(CommandHandler("setpairs", setpairs_cmd))
    application.add_handler(CommandHandler("setfreq", setfreq_cmd))
    application.add_handler(CommandHandler("setsens", setsens_cmd))
    application.add_handler(CommandHandler("setcat", setcat_cmd))
    application.add_handler(CommandHandler("testonce", testonce_cmd))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_pairs_message), group=2)

    try:
        bybit = BybitClient(proxy_url=settings.proxy_url) if settings else BybitClient()
    except Exception as e:
        log.error("Не удалось создать BybitClient: %s", e)
        bybit = BybitClient()

    try:
        for user_id, _pairs, freq, _sens, _cat in store.all_users():
            asyncio.get_event_loop().run_until_complete(scheduler.upsert_user_job(user_id, freq, check_job))
    except Exception as e:
        log.warning("Не удалось восстановить задачи: %s", e)

    log.info("Бот запущен. Ожидаю команды…")
    try:
        application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    except Conflict:
        log.error("Conflict: другой экземпляр уже запущен. Остановите его или смените токен.")
        raise

if __name__ == "__main__":
    main()
