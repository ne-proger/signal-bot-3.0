# src/main.py
from __future__ import annotations
import asyncio
import logging
import os
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    CallbackQueryHandler, MessageHandler, TypeHandler, filters
)
from telegram.error import Conflict

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bot")

# ---------------- SENTRY (optional) ----------------
def init_sentry():
    dsn = (os.getenv("SENTRY_DSN") or "").strip()
    if not dsn:
        log.info("Sentry: disabled (no DSN).")
        return False
    try:
        import sentry_sdk  # noqa
        sentry_sdk.init(dsn=dsn, environment=os.getenv("SENTRY_ENV", "prod"))
        log.info("Sentry: initialized.")
        return True
    except Exception as e:
        log.warning("Sentry: init failed: %s", e)
        return False

async def sentrytest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        import sentry_sdk
        sentry_sdk.capture_message("🔔 Manual test message from /sentrytest")
        sentry_sdk.flush(timeout=5)
        await update.message.reply_text("Готово. Проверь Sentry → Issues.")
    except Exception as e:
        await update.message.reply_text(f"Sentry недоступен: {e}")

async def sentryboom_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💥 Генерирую исключение и отправляю в Sentry…")
    try:
        1 / 0
    except Exception as e:
        try:
            import sentry_sdk
            sentry_sdk.capture_exception(e)
            sentry_sdk.flush(timeout=5)
            await update.message.reply_text("Готово. Проверь Sentry → Issues.")
        except Exception as sx:
            await update.message.reply_text(f"Sentry недоступен: {sx}")

# ---------------- COMMON (both modes) ----------------
WELCOME_SAFE = (
    "Привет! Я крипто-бот.\n"
    "Основные команды:\n"
    "/start, /ping, /diag, /sentrytest, /sentryboom\n\n"
    "Сейчас бот в SAFE_MODE — доступны только базовые команды.\n"
)

WELCOME_FULL = (
    "Привет! Я крипто-бот.\n"
    "Команды:\n"
    "/settings — меню настроек (кнопки)\n"
    "/debugbtn — тест кнопок\n"
    "/history — последние сигналы\n"
    "/setpairs BTCUSDT,TRXUSDT — задать пары\n"
    "/setfreq 5m|1h|1d — периодичность\n"
    "/setsens low|medium|high — чувствительность\n"
    "/setcat spot|linear — категория рынка\n"
    "/status — показать настройки\n"
    "/testonce — запустить проверку сейчас\n"
    "/diag — диагностика (БД/сборка)\n"
    "/sentrytest — отправить тестовое событие в Sentry\n"
    "/sentryboom — намеренно сгенерировать исключение (проверка Sentry)\n"
)

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def start_cmd_safe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_SAFE)

async def start_cmd_full(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_FULL)

async def diag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_url = os.getenv("DATABASE_URL", "")
    if db_url:
        db_url = db_url.replace(db_url.split("@")[0], "***://***:***")
    sentry_state = "on" if (os.getenv("SENTRY_DSN") or "").strip() else "off"
    safe = os.getenv("SAFE_MODE", "1")
    build_ts = os.getenv("BUILD_AT", "") or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    await update.message.reply_text(
        "🧪 DIAG\n"
        f"SAFE_MODE: {safe}\n"
        f"DB: {'Postgres' if os.getenv('DATABASE_URL') else 'SQLite/—'}\n"
        f"DATABASE_URL: {db_url or '—'}\n"
        f"Sentry: {sentry_state}\n"
        f"Build at: {build_ts}"
    )

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Exception in handler", exc_info=context.error)
    try:
        import sentry_sdk
        if context.error:
            sentry_sdk.capture_exception(context.error)
            sentry_sdk.flush(timeout=5)
    except Exception:
        pass

# ---------------- FULL MODE (all features) ----------------
def run_full(application, settings):
    """
    В полном режиме загружаем тяжёлые зависимости и регистрируем все команды.
    """
    log.info("[FULL] Boot: importing modules…")
    from src.storage import Storage
    from src.scheduler import BotScheduler
    from src.utils import (
        parse_frequency, norm_pairs, validate_sensitivity, ParseError,
        validate_category, CONF_THRESHOLDS
    )
    from .analysis import analyze_and_decide

    # Bybit fallback
    try:
        from .bybit_client import BybitClient  # type: ignore
    except Exception:
        class BybitClient:  # type: ignore
            def __init__(self, proxy_url: str | None = None): self.proxy_url = proxy_url
            def latest_ohlcv_pack(self, symbol: str, category: str = "spot"):
                return {"W": [], "D": [], "240": []}

    # ------- состояние -------
    store: Optional[Storage] = None
    scheduler: Optional[BotScheduler] = None
    bybit: Optional[BybitClient] = None

    # ---------- handlers ----------
    async def ensure_user_row(user_id: int):
        assert store is not None and scheduler is not None
        if store.get_user(user_id) is None:
            store.upsert_user(user_id)
            await scheduler.upsert_user_job(user_id, 3600, check_job)

    def _build_settings_keyboard(current: dict) -> InlineKeyboardMarkup:
        FREQ_PRESETS = [("1m", "60"), ("5m", "300"), ("15m", "900"), ("1h", "3600"), ("4h", "14400"), ("1d", "86400")]
        sens = current.get("sensitivity", "medium")
        cat = current.get("category", "spot")
        freq_row = [InlineKeyboardButton(txt, callback_data=f"freq:{sec}") for (txt, sec) in FREQ_PRESETS]
        sens_row = [
            InlineKeyboardButton(("• " if sens == "low" else "") + "low", callback_data="sens:low"),
            InlineKeyboardButton(("• " if sens == "medium" else "") + "medium", callback_data="sens:medium"),
            InlineKeyboardButton(("• " if sens == "high" else "") + "high", callback_data="sens:high"),
        ]
        cat_row = [
            InlineKeyboardButton(("• " if cat == "spot" else "") + "spot", callback_data="cat:spot"),
            InlineKeyboardButton(("• " if cat == "linear" else "") + "linear", callback_data="cat:linear"),
        ]
        pairs_row = [InlineKeyboardButton("✏️ Изменить пары", callback_data="pairs:edit")]
        return InlineKeyboardMarkup([freq_row, sens_row, cat_row, pairs_row])

    async def _send_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await application.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=kb)

    async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(WELCOME_FULL)

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

    async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        assert update.effective_user and update.message and store is not None
        rows = store.recent_signals(user_id=update.effective_user.id, limit=20)
        if not rows:
            await update.message.reply_text("История сигналов пуста.")
            return
        lines = ["🗂 Последние сигналы:"]
        for r in rows:
            ts = int(r["created_at"])
            sym = r["symbol"]
            conf = r["confidence"]
            entry = r["entry"]; tp = r["take_profit"]; sl = r["stop_loss"]
            horizon = r["exit_horizon"]
            lines.append(
                f"• {sym} | conf={conf if conf is not None else '—'} | "
                f"entry={entry} | tp={tp} | sl={sl} | h={horizon} | t={ts}"
            )
        await update.message.reply_text("\n".join(lines))

    async def setpairs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        assert update.effective_user and update.message
        await ensure_user_row(update.effective_user.id)
        if not context.args:
            await update.message.reply_text("Укажите пары через запятую, например: /setpairs BTCUSDT,TRXUSDT")
            return
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
        store.upsert_user(update.effective_user.id, category=cat)
        await update.message.reply_text(f"Ок. Категория: {cat}")

    async def testonce_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        assert update.effective_user and update.message
        await run_check_for_user(update.effective_user.id, context)
        await update.message.reply_text("Тестовая проверка выполнена.")

    async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        assert update.callback_query and update.effective_user
        q = update.callback_query
        data = (q.data or "").strip()
        try:
            await q.answer(text=f"callback: {data}", show_alert=False)
        except Exception:
            pass
        try:
            if data.startswith("freq:"):
                seconds = int(data.split(":", 1)[1])
                store.upsert_user(update.effective_user.id, frequency_seconds=seconds)
                await scheduler.upsert_user_job(update.effective_user.id, seconds, check_job)
                await application.bot.send_message(chat_id=update.effective_chat.id, text=f"⏱ Периодичность: {seconds} сек")
            elif data.startswith("sens:"):
                val = validate_sensitivity(data.split(":", 1)[1])
                store.upsert_user(update.effective_user.id, sensitivity=val)
                await application.bot.send_message(chat_id=update.effective_chat.id, text=f"🎚 Чувствительность: {val}")
            elif data.startswith("cat:"):
                cat = validate_category(data.split(":", 1)[1])
                store.upsert_user(update.effective_user.id, category=cat)
                await application.bot.send_message(chat_id=update.effective_chat.id, text=f"🪙 Категория: {cat}")
            elif data == "dbg:ping":
                await application.bot.send_message(chat_id=update.effective_chat.id, text="🏓 pong")
            elif data == "pairs:edit":
                context.user_data["await_pairs"] = True
                await application.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="Введите пары через запятую, например: BTCUSDT,TRXUSDT,INJUSDT",
                    reply_markup=ReplyKeyboardRemove()
                )
            else:
                await application.bot.send_message(chat_id=update.effective_chat.id, text="Неизвестная команда кнопки.")
        except ParseError as e:
            await application.bot.send_message(chat_id=update.effective_chat.id, text=f"Ошибка: {e}")

        await _send_settings_menu(update, context)

    async def on_pairs_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.user_data.get("await_pairs"):
            return
        pairs = norm_pairs(update.message.text or "")
        store.upsert_user(update.effective_user.id, pairs=pairs)
        context.user_data["await_pairs"] = False
        await update.message.reply_text(f"✅ Пары обновлены: {pairs}", reply_markup=ReplyKeyboardRemove())
        await _send_settings_menu(update, context)

    # ---- jobs / analysis ----
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

            try:
                res = analyze_and_decide(
                    symbol=sym,
                    ohlcv_pack=pack,
                    ma_window=21,
                    macd_cfg={"fast": 12, "slow": 26, "signal": 9},
                    sensitivity=sens,
                    model=os.getenv("OPENAI_MODEL", "gpt-4o-mini-2024-08-06"),
                    book_url=os.getenv("LITERATURE_URLS"),
                )
            except Exception as e:
                lines.append(f"• {sym}: ошибка анализа: {e}")
                continue

            buy = bool(res.get("buy_signal"))
            rationale = res.get("rationale", "")

            if buy:
                conf = float(res.get("confidence") or 0.0)
                entry = res.get("entry"); tp = res.get("take_profit"); sl = res.get("stop_loss"); horizon = res.get("exit_horizon")

                if conf >= conf_threshold:
                    # публикация
                    msg = (
                        f"🔔 SIGNAL BUY — {sym}\n"
                        f"entry: {entry}\n"
                        f"take_profit: {tp}\n"
                        f"stop_loss: {sl}\n"
                        f"exit_horizon: {horizon}\n"
                        f"confidence: {conf:.2f}\n"
                        f"rationale: {rationale}"
                    )
                    await application.bot.send_message(chat_id=user_id, text=msg)
                    # лог в БД
                    store.log_signal(
                        user_id=user_id, symbol=sym, signal_type="buy",
                        confidence=conf,
                        entry=(float(entry) if entry is not None else None),
                        take_profit=(float(tp) if tp is not None else None),
                        stop_loss=(float(sl) if sl is not None else None),
                        exit_horizon=(horizon if horizon is not None else None),
                    )
                else:
                    await application.bot.send_message(
                        chat_id=user_id,
                        text=f"[FILTERED] {sym}: buy confidence {conf:.2f} ниже порога {conf_threshold:.2f} для {sens}"
                    )
            else:
                await application.bot.send_message(chat_id=user_id, text=f"[NO SIGNAL] {sym}: {rationale}")

        if lines:
            try:
                await application.bot.send_message(chat_id=user_id, text="\n".join(lines))
            except Exception as e:
                log.error("Не удалось отправить сводку пользователю %s: %s", user_id, e)

    # ----- Инициализация и регистрация -----
    log.info("[FULL] Step 1/4: Settings already loaded.")

    log.info("[FULL] Step 2/4: init Storage & Scheduler…")
    store = Storage()
    scheduler = BotScheduler(application)

    log.info("[FULL] Step 3/4: init BybitClient…")
    bybit = BybitClient(proxy_url=getattr(settings, "proxy_url", None))  # type: ignore

    log.info("[FULL] Step 4/4: register handlers…")
    # команды
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("settings", settings_cmd))
    application.add_handler(CommandHandler("debugbtn", debugbtn_cmd))
    application.add_handler(CommandHandler("history", history_cmd))
    application.add_handler(CommandHandler("setpairs", setpairs_cmd))
    application.add_handler(CommandHandler("setfreq", setfreq_cmd))
    application.add_handler(CommandHandler("setsens", setsens_cmd))
    application.add_handler(CommandHandler("setcat", setcat_cmd))
    application.add_handler(CommandHandler("testonce", testonce_cmd))
    # для ввода пар после pairs:edit
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_pairs_message), group=2)
    # callbacks
    application.add_handler(CallbackQueryHandler(on_callback, pattern=".*"), group=0)

    # восстановление задач
    try:
        for user_id, _pairs, freq, _sens, _cat in store.all_users():  # type: ignore
            asyncio.get_event_loop().run_until_complete(
                scheduler.upsert_user_job(user_id, freq, check_job)  # type: ignore
            )
        log.info("[FULL] Jobs restored.")
    except Exception as e:
        log.warning("[FULL] restore jobs failed: %s", e)


# ---------------- MAIN ----------------
async def _preflight(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook deleted (preflight).")
    except Exception as e:
        log.warning("delete_webhook failed: %s", e)

def main():
    load_dotenv()
    init_sentry()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")

    application = ApplicationBuilder().token(token).concurrent_updates(True).build()
    application.add_error_handler(error_handler)

    # Общие команды (есть в обоих режимах)
    application.add_handler(TypeHandler(Update, lambda u, c: None), group=1)  # «тихий» логгер
    application.add_handler(CommandHandler("ping", ping_cmd))
    application.add_handler(CommandHandler("diag", diag_cmd))
    application.add_handler(CommandHandler("sentrytest", sentrytest_cmd))
    application.add_handler(CommandHandler("sentryboom", sentryboom_cmd))

    # Режим
    safe_mode = os.getenv("SAFE_MODE", "1").strip()
    log.info("SAFE_MODE=%s", safe_mode)

    if safe_mode == "0":
        from src.config import Settings
        settings = Settings.load()
        run_full(application, settings)
    else:
        application.add_handler(CommandHandler("start", start_cmd_safe))
        log.info("Running in SAFE_MODE: только базовые команды.")

    # Префлайт и запуск
    asyncio.get_event_loop().run_until_complete(_preflight(application))
    log.info("Бот запускается…")
    try:
        application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    except Conflict:
        log.error("Conflict: другой экземпляр уже запущен. Остановите его или смените токен.")
        raise

if __name__ == "__main__":
    main()
