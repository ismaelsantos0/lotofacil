import logging
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from scraper_caixa import fetch_latest_results

# =========================
# CONFIG
# =========================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TZ_NAME = os.getenv("TZ", "America/Boa_Vista")
DEFAULT_LOOKBACK = int(os.getenv("DEFAULT_LOOKBACK", "5"))

UPDATE_HOUR = int(os.getenv("UPDATE_HOUR", "0"))
UPDATE_MINUTE = int(os.getenv("UPDATE_MINUTE", "10"))

REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "19"))
REMINDER_MINUTE = int(os.getenv("REMINDER_MINUTE", "30"))

DB_PATH = "bot.db"

if not TOKEN:
    raise RuntimeError("Defina a variável de ambiente TELEGRAM_BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("lotofacil-bot")

LATEST_ANALYSIS = None


# =========================
# DB
# =========================
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id INTEGER PRIMARY KEY
            )
            """
        )
        conn.commit()


def add_subscriber(chat_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO subscribers (chat_id) VALUES (?)",
            (chat_id,),
        )
        conn.commit()


def remove_subscriber(chat_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
        conn.commit()


def list_subscribers() -> list[int]:
    with get_conn() as conn:
        rows = conn.execute("SELECT chat_id FROM subscribers").fetchall()
        return [int(r["chat_id"]) for r in rows]


# =========================
# MODELOS
# =========================
@dataclass
class Concurso:
    numero: int
    data: str
    dezenas: list[int]


@dataclass
class Analise:
    concursos: list[Concurso]
    ranking: list[int]
    d1: list[int]
    d2: list[int]
    d3: list[int]
    d4: list[int]
    j1: list[int]
    j2: list[int]
    j3: list[int]
    j4: list[int]


# =========================
# LÓGICA
# =========================
def build_analysis(lookback: int = 5) -> Analise:
    raw_concursos = fetch_latest_results(limit=lookback)

    concursos = [
        Concurso(numero=c.numero, data=c.data, dezenas=sorted(c.dezenas))
        for c in raw_concursos
    ]

    freq = Counter()
    recency_score = Counter()

    for idx, concurso in enumerate(concursos):
        weight = lookback - idx
        for dezena in concurso.dezenas:
            freq[dezena] += 1
            recency_score[dezena] += weight

    universo = list(range(1, 26))
    ranking = sorted(
        universo,
        key=lambda n: (-freq[n], -recency_score[n], n)
    )

    d1 = sorted(ranking[:10])
    d2 = sorted(ranking[10:15])
    d3 = sorted(ranking[15:20])
    d4 = sorted(ranking[20:25])

    j1 = sorted(d1 + d2)
    j2 = sorted(d1 + d3)
    j3 = sorted(d1 + d4)
    j4 = sorted(d2 + d3 + d4)

    return Analise(
        concursos=concursos,
        ranking=ranking,
        d1=d1,
        d2=d2,
        d3=d3,
        d4=d4,
        j1=j1,
        j2=j2,
        j3=j3,
        j4=j4,
    )


def fmt_nums(nums: list[int]) -> str:
    return " ".join(f"{n:02d}" for n in nums)


def render_analysis(a: Analise, lookback: int) -> str:
    concursos_txt = "\n".join(
        f"• Concurso {c.numero} ({c.data}): {fmt_nums(c.dezenas)}"
        for c in a.concursos
    )

    return (
        f"🎯 <b>Lotofácil | Estratégia diária</b>\n"
        f"Base usada: últimos <b>{lookback}</b> concursos\n\n"
        f"<b>Concursos analisados</b>\n{concursos_txt}\n\n"
        f"<b>D1</b> (10 mais quentes)\n{fmt_nums(a.d1)}\n\n"
        f"<b>D2</b> (próximos 5)\n{fmt_nums(a.d2)}\n\n"
        f"<b>D3</b> (próximos 5)\n{fmt_nums(a.d3)}\n\n"
        f"<b>D4</b> (5 restantes)\n{fmt_nums(a.d4)}\n\n"
        f"<b>Jogos gerados</b>\n"
        f"J1 = D1 + D2\n{fmt_nums(a.j1)}\n\n"
        f"J2 = D1 + D3\n{fmt_nums(a.j2)}\n\n"
        f"J3 = D1 + D4\n{fmt_nums(a.j3)}\n\n"
        f"J4 = D2 + D3 + D4\n{fmt_nums(a.j4)}"
    )


# =========================
# JOBS
# =========================
async def update_results_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    global LATEST_ANALYSIS
    try:
        logger.info("Atualizando resultados da Lotofácil...")
        LATEST_ANALYSIS = build_analysis(lookback=DEFAULT_LOOKBACK)
        logger.info("Resultados atualizados com sucesso.")
    except Exception as e:
        logger.exception("Erro ao atualizar resultados: %s", e)


async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    subscribers = list_subscribers()
    if not subscribers:
        logger.info("Nenhum inscrito para lembrete.")
        return

    text = "⏰ Lembrete: os resultados já foram atualizados. Use /hoje para gerar seus jogos da Lotofácil."

    for chat_id in subscribers:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.exception("Erro ao enviar lembrete para %s: %s", chat_id, e)


# =========================
# COMANDOS
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return

    add_subscriber(update.effective_chat.id)
    msg = (
        "✅ Você foi inscrito nos lembretes.\n\n"
        "Comandos:\n"
        "/hoje - gera os jogos com base atualizada\n"
        "/ultimos5 - força análise com 5 concursos\n"
        "/ultimos10 - força análise com 10 concursos\n"
        "/status - mostra inscritos\n"
        "/stop - desativa lembretes"
    )
    await update.message.reply_text(msg)


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return

    remove_subscriber(update.effective_chat.id)
    await update.message.reply_text("🛑 Lembretes desativados para este chat.")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    total = len(list_subscribers())
    await update.message.reply_text(f"👥 Inscritos nos lembretes: {total}")


async def hoje_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global LATEST_ANALYSIS

    if update.message is None:
        return

    try:
        if LATEST_ANALYSIS is None:
            LATEST_ANALYSIS = build_analysis(lookback=DEFAULT_LOOKBACK)

        await update.message.reply_text(
            render_analysis(LATEST_ANALYSIS, DEFAULT_LOOKBACK),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.exception("Erro no /hoje")
        await update.message.reply_text(f"❌ Erro ao gerar os jogos: {e}")


async def ultimos5_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    try:
        analysis = build_analysis(lookback=5)
        await update.message.reply_text(
            render_analysis(analysis, 5),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.exception("Erro no /ultimos5")
        await update.message.reply_text(f"❌ Erro ao gerar os jogos: {e}")


async def ultimos10_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    try:
        analysis = build_analysis(lookback=10)
        await update.message.reply_text(
            render_analysis(analysis, 10),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.exception("Erro no /ultimos10")
        await update.message.reply_text(f"❌ Erro ao gerar os jogos: {e}")


# =========================
# MAIN
# =========================
def main() -> None:
    init_db()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("hoje", hoje_cmd))
    app.add_handler(CommandHandler("ultimos5", ultimos5_cmd))
    app.add_handler(CommandHandler("ultimos10", ultimos10_cmd))

    tz = ZoneInfo(TZ_NAME)

    update_time = time(hour=UPDATE_HOUR, minute=UPDATE_MINUTE, tzinfo=tz)
    reminder_time = time(hour=REMINDER_HOUR, minute=REMINDER_MINUTE, tzinfo=tz)

    if app.job_queue is None:
        raise RuntimeError(
            "JobQueue não disponível. Verifique o requirements com python-telegram-bot[job-queue]."
        )

    app.job_queue.run_daily(
        callback=update_results_job,
        time=update_time,
        name="update_results_job",
    )

    app.job_queue.run_daily(
        callback=reminder_job,
        time=reminder_time,
        name="reminder_job",
    )

    logger.info(
        "Bot iniciado. Atualização diária às %02d:%02d e lembrete às %02d:%02d (%s)",
        UPDATE_HOUR,
        UPDATE_MINUTE,
        REMINDER_HOUR,
        REMINDER_MINUTE,
        TZ_NAME,
    )

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
