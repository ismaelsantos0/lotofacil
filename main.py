import json
import logging
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import Conflict
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from fetch_api import fetch_latest_results

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

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "1800"))

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

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                target_concurso INTEGER NOT NULL,
                lookback INTEGER NOT NULL,
                generated_at TEXT NOT NULL,
                games_json TEXT NOT NULL,
                checked INTEGER NOT NULL DEFAULT 0,
                checked_at TEXT,
                result_concurso INTEGER,
                result_data TEXT,
                result_dezenas TEXT
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


def save_prediction(chat_id: int, analysis: "Analise", lookback: int) -> int:
    tz = ZoneInfo(TZ_NAME)
    generated_at = datetime.now(tz).isoformat(timespec="seconds")
    target_concurso = analysis.concursos[0].numero + 1

    games = {
        "J1": analysis.j1,
        "J2": analysis.j2,
        "J3": analysis.j3,
        "J4": analysis.j4,
    }

    with get_conn() as conn:
        # evita duplicar a mesma estratégia pendente para o mesmo chat/concurso/lookback
        conn.execute(
            """
            DELETE FROM predictions
            WHERE chat_id = ?
              AND target_concurso = ?
              AND lookback = ?
              AND checked = 0
            """,
            (chat_id, target_concurso, lookback),
        )

        cur = conn.execute(
            """
            INSERT INTO predictions (
                chat_id,
                target_concurso,
                lookback,
                generated_at,
                games_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                target_concurso,
                lookback,
                generated_at,
                json.dumps(games, ensure_ascii=False),
            ),
        )
        conn.commit()
        return target_concurso


def list_pending_predictions() -> list[sqlite3.Row]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM predictions
            WHERE checked = 0
            ORDER BY target_concurso ASC, id ASC
            """
        ).fetchall()
        return rows


def count_pending_predictions_for_chat(chat_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM predictions
            WHERE chat_id = ?
              AND checked = 0
            """,
            (chat_id,),
        ).fetchone()
        return int(row["total"] or 0)


def mark_prediction_checked(
    prediction_id: int,
    result_concurso: int,
    result_data: str,
    result_dezenas: list[int],
) -> None:
    tz = ZoneInfo(TZ_NAME)
    checked_at = datetime.now(tz).isoformat(timespec="seconds")

    with get_conn() as conn:
        conn.execute(
            """
            UPDATE predictions
            SET checked = 1,
                checked_at = ?,
                result_concurso = ?,
                result_data = ?,
                result_dezenas = ?
            WHERE id = ?
            """,
            (
                checked_at,
                result_concurso,
                result_data,
                json.dumps(result_dezenas, ensure_ascii=False),
                prediction_id,
            ),
        )
        conn.commit()


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
async def build_analysis(lookback: int = 5) -> Analise:
    raw_concursos = fetch_latest_results(limit=lookback)

    concursos = [
        Concurso(
            numero=int(c["numero"]),
            data=str(c["data"]),
            dezenas=sorted(int(n) for n in c["dezenas"]),
        )
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
    ranking = sorted(universo, key=lambda n: (-freq[n], -recency_score[n], n))

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


# =========================
# FORMATAÇÃO
# =========================
FULLWIDTH_MAP = str.maketrans("0123456789", "０１２３４５６７８９")


def to_fullwidth(value: str) -> str:
    return value.translate(FULLWIDTH_MAP)


def fmt_num(n: int) -> str:
    return to_fullwidth(f"{n:02d}")


def fmt_plain_num(n: int) -> str:
    return f"{n:02d}"


def fmt_nums(nums: list[int]) -> str:
    return " ".join(fmt_num(n) for n in nums)


def fmt_nums_multiline(nums: list[int], first_line: int = 8) -> str:
    line1 = " ".join(fmt_num(n) for n in nums[:first_line])
    line2 = " ".join(fmt_num(n) for n in nums[first_line:])
    return f"{line1}\n{line2}"


def fmt_hits(nums: list[int], result_nums: list[int]) -> str:
    acertadas = sorted(set(nums) & set(result_nums))
    if not acertadas:
        return "—"
    return " ".join(fmt_num(n) for n in acertadas)


def render_analysis(a: Analise, lookback: int, target_concurso: int) -> str:
    return (
        f"🎯 <b>Lotofácil | Jogos do dia</b>\n\n"
        f"📊 Base: <b>{fmt_num(lookback)}</b> concursos\n"
        f"🔥 Quentes:\n"
        f"{fmt_nums(a.d1)}\n\n\n"
        f"🎟 <b>J1</b>\n"
        f"{fmt_nums_multiline(a.j1)}\n\n\n"
        f"🎟 <b>J2</b>\n"
        f"{fmt_nums_multiline(a.j2)}\n\n\n"
        f"🎟 <b>J3</b>\n"
        f"{fmt_nums_multiline(a.j3)}\n\n\n"
        f"🎟 <b>J4</b>\n"
        f"{fmt_nums_multiline(a.j4)}\n\n"
        f"🗂️ Conferência automática: concurso <b>{to_fullwidth(str(target_concurso))}</b>"
    )


def render_result_check(prediction: sqlite3.Row, result_concurso: int, result_data: str, result_nums: list[int]) -> str:
    games = json.loads(prediction["games_json"])

    blocks = []
    best_hits = 0

    for name in ("J1", "J2", "J3", "J4"):
        nums = sorted(int(n) for n in games[name])
        hits = len(set(nums) & set(result_nums))
        best_hits = max(best_hits, hits)

        medal = "🏆" if hits >= 11 else "🎟"
        blocks.append(
            f"{medal} <b>{name}</b> — <b>{fmt_plain_num(hits)}</b> acertos\n"
            f"{fmt_nums_multiline(nums)}\n"
            f"✅ Acertadas: {fmt_hits(nums, result_nums)}"
        )

    resumo = "🚀 Bateu premiação!" if best_hits >= 11 else "📌 Resultado conferido"

    return (
        f"{resumo}\n\n"
        f"🎯 Concurso <b>{to_fullwidth(str(result_concurso))}</b> ({result_data})\n"
        f"🔢 Resultado:\n"
        f"{fmt_nums_multiline(result_nums)}\n\n"
        + "\n\n".join(blocks)
    )


# =========================
# JOBS
# =========================
async def update_results_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    global LATEST_ANALYSIS
    try:
        logger.info("Atualizando resultados da Lotofácil...")
        LATEST_ANALYSIS = await build_analysis(lookback=DEFAULT_LOOKBACK)
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


async def check_results_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = list_pending_predictions()
    if not pending:
        return

    try:
        latest_results = fetch_latest_results(limit=15)
    except Exception as e:
        logger.exception("Erro ao buscar resultados para conferência: %s", e)
        return

    result_map: dict[int, dict] = {}
    for item in latest_results:
        numero = int(item["numero"])
        result_map[numero] = {
            "numero": numero,
            "data": str(item["data"]),
            "dezenas": sorted(int(n) for n in item["dezenas"]),
        }

    for prediction in pending:
        target_concurso = int(prediction["target_concurso"])
        found = result_map.get(target_concurso)

        if not found:
            continue

        text = render_result_check(
            prediction=prediction,
            result_concurso=found["numero"],
            result_data=found["data"],
            result_nums=found["dezenas"],
        )

        try:
            await context.bot.send_message(
                chat_id=int(prediction["chat_id"]),
                text=text,
                parse_mode=ParseMode.HTML,
            )
            mark_prediction_checked(
                prediction_id=int(prediction["id"]),
                result_concurso=found["numero"],
                result_data=found["data"],
                result_dezenas=found["dezenas"],
            )
            logger.info(
                "Conferência enviada para chat %s do concurso %s",
                prediction["chat_id"],
                target_concurso,
            )
        except Exception as e:
            logger.exception(
                "Erro ao enviar conferência para chat %s: %s",
                prediction["chat_id"],
                e,
            )


# =========================
# ENVIO + SALVAMENTO
# =========================
async def send_analysis_and_store(
    update: Update,
    analysis: Analise,
    lookback: int,
) -> None:
    if update.message is None or update.effective_chat is None:
        return

    target_concurso = save_prediction(
        chat_id=update.effective_chat.id,
        analysis=analysis,
        lookback=lookback,
    )

    await update.message.reply_text(
        render_analysis(analysis, lookback, target_concurso),
        parse_mode=ParseMode.HTML,
    )


# =========================
# COMANDOS
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return

    add_subscriber(update.effective_chat.id)
    msg = (
        "✅ Você foi inscrito nos lembretes.\n\n"
        "Comandos disponíveis:\n"
        "/hoje - gera os jogos com base atualizada\n"
        "/ultimos5 - força análise com 5 concursos\n"
        "/ultimos10 - força análise com 10 concursos\n"
        "/atualizar - atualiza agora a base\n"
        "/status - mostra inscritos e pendências\n"
        "/stop - desativa lembretes"
    )
    await update.message.reply_text(msg)


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return

    remove_subscriber(update.effective_chat.id)
    await update.message.reply_text("🛑 Lembretes desativados para este chat.")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_chat is None:
        return

    total = len(list_subscribers())
    pendentes = count_pending_predictions_for_chat(update.effective_chat.id)
    await update.message.reply_text(
        f"👥 Inscritos nos lembretes: {total}\n"
        f"🎟 Jogos pendentes de conferência neste chat: {pendentes}"
    )


async def atualizar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global LATEST_ANALYSIS

    if update.message is None:
        return

    try:
        LATEST_ANALYSIS = await build_analysis(lookback=DEFAULT_LOOKBACK)
        await update.message.reply_text("✅ Base atualizada com sucesso.")
    except Exception as e:
        logger.exception("Erro no /atualizar")
        await update.message.reply_text(f"❌ Erro ao atualizar a base: {e}")


async def hoje_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global LATEST_ANALYSIS

    if update.message is None:
        return

    try:
        if LATEST_ANALYSIS is None:
            LATEST_ANALYSIS = await build_analysis(lookback=DEFAULT_LOOKBACK)

        await send_analysis_and_store(update, LATEST_ANALYSIS, DEFAULT_LOOKBACK)
    except Exception as e:
        logger.exception("Erro no /hoje")
        await update.message.reply_text(f"❌ Erro ao gerar os jogos: {e}")


async def ultimos5_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    try:
        analysis = await build_analysis(lookback=5)
        await send_analysis_and_store(update, analysis, 5)
    except Exception as e:
        logger.exception("Erro no /ultimos5")
        await update.message.reply_text(f"❌ Erro ao gerar os jogos: {e}")


async def ultimos10_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    try:
        analysis = await build_analysis(lookback=10)
        await send_analysis_and_store(update, analysis, 10)
    except Exception as e:
        logger.exception("Erro no /ultimos10")
        await update.message.reply_text(f"❌ Erro ao gerar os jogos: {e}")


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text("🏓 Pong")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        logger.warning("Conflito temporário de polling detectado.")
        return
    logger.exception("Erro não tratado:", exc_info=context.error)


# =========================
# MAIN
# =========================
def main() -> None:
    init_db()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("atualizar", atualizar_cmd))
    app.add_handler(CommandHandler("hoje", hoje_cmd))
    app.add_handler(CommandHandler("ultimos5", ultimos5_cmd))
    app.add_handler(CommandHandler("ultimos10", ultimos10_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_error_handler(error_handler)

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

    app.job_queue.run_repeating(
        callback=check_results_job,
        interval=CHECK_INTERVAL_SECONDS,
        first=60,
        name="check_results_job",
    )

    logger.info(
        "Bot iniciado. Atualização diária às %02d:%02d, lembrete às %02d:%02d e conferência a cada %s segundos (%s)",
        UPDATE_HOUR,
        UPDATE_MINUTE,
        REMINDER_HOUR,
        REMINDER_MINUTE,
        CHECK_INTERVAL_SECONDS,
        TZ_NAME,
    )

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
