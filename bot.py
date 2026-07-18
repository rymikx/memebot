"""
bot.py — Telegram-бот для анализа мем-токенов.

Запуск:
    export TELEGRAM_BOT_TOKEN="твой_токен_от_BotFather"
    python bot.py

Требует: python-telegram-bot>=21, aiohttp (см. requirements.txt)
"""

import logging
import os
import re

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from analyzer import analyze_token, AnalysisError
from keep_alive import start_keep_alive_server

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

EVM_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")  # покрывает и Robinhood Chain (тоже EVM)
SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
# TON: user-friendly формат — 48 символов base64url, обычно начинается с
# EQ/UQ (mainnet) или kQ/0Q (testnet); либо raw-формат "0:<64 hex>"
TON_ADDRESS_RE = re.compile(r"^[EUk0][Qf][A-Za-z0-9_-]{46}$|^-?\d:[a-fA-F0-9]{64}$")

DISCLAIMER = (
    "\n\n⚠️ Это не финансовый совет. Скор основан на автоматической проверке "
    "ликвидности, контракта и держателей — он НЕ гарантирует безопасность токена "
    "и не учитывает хайп в соцсетях, репутацию команды и рыночные условия. "
    "Большинство мем-коинов теряют почти всю стоимость — вкладывай только то, "
    "что готов потерять полностью."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Пришли мне адрес контракта мем-токена — поддерживаю Ethereum, BSC, Base, "
        "Polygon, Arbitrum, Avalanche, Optimism, Fantom, Robinhood Chain, Solana и TON.\n\n"
        "Разберу его по ликвидности, объёму, безопасности контракта, держателям и "
        "оценю по шкале 0-10.\n\n"
        "⚠️ Для TON и Robinhood Chain проверка безопасности контракта (GoPlus) пока "
        "может быть недоступна — это новые/нестандартные сети, и не все проверки "
        "покрыты. В таком случае бот честно покажет N/A по этим пунктам, а не "
        "выдумает оценку.\n\n"
        "Просто отправь адрес сообщением."
    )


def format_report(result: dict) -> str:
    lines = []
    symbol = result.get("symbol") or "?"
    name = result.get("name") or ""
    lines.append(f"<b>{symbol}</b> ({name})")
    lines.append(f"Сеть: {result['chain']} | DEX: {result.get('dex', '?')}")
    if result.get("price_usd"):
        lines.append(f"Цена: ${result['price_usd']}")
    if result.get("fdv"):
        lines.append(f"FDV/MCap: ${result['fdv']:,.0f}")
    lines.append("")
    lines.append("<b>Разбор по критериям:</b>")

    for label, (score, note) in result["components"].items():
        score_str = f"{score:.1f}/2.0" if score is not None else "N/A"
        lines.append(f"• <b>{label}</b>: {score_str}\n  {note}")

    lines.append("")
    lines.append(f"<b>⏱ Тайминг входа:</b> {result['timing_note']}")
    lines.append("")

    if result["final_score"] is not None:
        lines.append(f"<b>🎯 Итоговый скор: {result['final_score']}/10</b>")
        if result["unavailable_count"] > 0:
            lines.append(
                f"(рассчитан по {5 - result['unavailable_count']} из 5 доступных критериев)"
            )
    else:
        lines.append("<b>🎯 Итоговый скор: не удалось рассчитать</b> — нет данных ни по одному критерию")

    if result.get("url"):
        lines.append(f"\n<a href='{result['url']}'>Открыть на DexScreener</a>")

    lines.append(DISCLAIMER)
    return "\n".join(lines)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if not (
        EVM_ADDRESS_RE.match(text)
        or SOLANA_ADDRESS_RE.match(text)
        or TON_ADDRESS_RE.match(text)
    ):
        await update.message.reply_text(
            "Это не похоже на адрес токена. Пришли адрес контракта:\n"
            "• 0x... — для EVM-сетей (Ethereum/BSC/Base/Robinhood Chain и т.д.)\n"
            "• base58-адрес — для Solana\n"
            "• EQ.../UQ... или 0:... — для TON"
        )
        return

    status_msg = await update.message.reply_text("Анализирую... 🔍")

    try:
        result = await analyze_token(text)
    except AnalysisError as e:
        await status_msg.edit_text(f"Не получилось: {e}")
        return
    except Exception:
        logger.exception("Unexpected error analyzing %s", text)
        await status_msg.edit_text(
            "Произошла непредвиденная ошибка при анализе. Попробуй ещё раз чуть позже."
        )
        return

    report = format_report(result)
    await status_msg.edit_text(report, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "Не найден TELEGRAM_BOT_TOKEN. Установи переменную окружения:\n"
            "  export TELEGRAM_BOT_TOKEN='твой_токен'"
        )

    # Render (и некоторые другие PaaS) прокидывают переменную PORT и ждут,
    # что сервис слушает HTTP на этом порту. Наш бот работает через polling
    # и HTTP не отдаёт — поднимаем для этого отдельный лёгкий сервер.
    port = os.environ.get("PORT")
    if port:
        start_keep_alive_server(int(port))

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
