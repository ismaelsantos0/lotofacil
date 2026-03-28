import logging
import math
import os
import random
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time
from itertools import combinations
from zoneinfo import ZoneInfo

import psycopg2
from psycopg2.extras import Json, RealDictCursor

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import Conflict
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from fetch_api import fetch_latest_results

# =========================
# CONFIG
# =========================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TZ_NAME = os.getenv("TZ", "America/Boa_Vista")
DEFAULT_LOOKBACK = int(os.getenv("DEFAULT_LOOKBACK", "5"))

UPDATE_HOUR = int(os.getenv("UPDATE_HOUR", "0"))
UPDATE_MINUTE = int(os.getenv("UPDATE_MINUTE", "10"))

REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "19"))
REMINDER_MINUTE = int(os.getenv("REMINDER_MINUTE", "30"))

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "1800"))

if not TOKEN:
    raise RuntimeError("Defina a variável de ambiente TELEGRAM_BOT_TOKEN")

if not DATABASE_URL:
    raise RuntimeError("Defina a variável de ambiente DATABASE_URL")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("lotofacil-bot")

LATEST_ANALYSIS = None


# =========================
# DB
# =========================
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db() -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id BIGINT PRIMARY KEY
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS predictions (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    concurso_base INTEGER NOT NULL,
                    target_concurso INTEGER NOT NULL,
                    lookback INTEGER NOT NULL,
                    generated_at TIMESTAMPTZ NOT NULL,
                    games_json JSONB NOT NULL,
                    checked BOOLEAN NOT NULL DEFAULT FALSE,
                    checked_at TIMESTAMPTZ,
                    result_concurso INTEGER,
                    result_data TEXT,
                    result_dezenas JSONB,
                    hits_json JSONB,
                    best_hits INTEGER
                )
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_predictions_pending
                ON predictions (checked, target_concurso)
                """
            )

            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_prediction_open
                ON predictions (chat_id, target_concurso, lookback, checked)
                WHERE checked = FALSE
                """
            )

        conn.commit()


def add_subscriber(chat_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO subscribers (chat_id)
                VALUES (%s)
                ON CONFLICT (chat_id) DO NOTHING
                """,
                (chat_id,),
            )
        conn.commit()


def remove_subscriber(chat_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM subscribers WHERE chat_id = %s", (chat_id,))
        conn.commit()


def list_subscribers() -> list[int]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT chat_id FROM subscribers ORDER BY chat_id")
            rows = cur.fetchall()
            return [int(r["chat_id"]) for r in rows]


def save_prediction(chat_id: int, analysis: "Analise", lookback: int) -> int:
    tz = ZoneInfo(TZ_NAME)
    generated_at = datetime.now(tz)
    concurso_base = analysis.concursos[0].numero
    target_concurso = concurso_base + 1

    games = {
        "J1": analysis.j1,
        "J2": analysis.j2,
        "J3": analysis.j3,
        "J4": analysis.j4,
    }

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM predictions
                WHERE chat_id = %s
                  AND target_concurso = %s
                  AND lookback = %s
                  AND checked = FALSE
                """,
                (chat_id, target_concurso, lookback),
            )

            cur.execute(
                """
                INSERT INTO predictions (
                    chat_id,
                    concurso_base,
                    target_concurso,
                    lookback,
                    generated_at,
                    games_json
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    chat_id,
                    concurso_base,
                    target_concurso,
                    lookback,
                    generated_at,
                    Json(games),
                ),
            )
        conn.commit()

    return target_concurso


def list_pending_predictions() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT *
                FROM predictions
                WHERE checked = FALSE
                ORDER BY target_concurso ASC, id ASC
                """
            )
            return cur.fetchall()


def count_pending_predictions_for_chat(chat_id: int) -> int:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM predictions
                WHERE chat_id = %s
                  AND checked = FALSE
                """,
                (chat_id,),
            )
            row = cur.fetchone()
            return int(row["total"] or 0)


def mark_prediction_checked(
    prediction_id: int,
    result_concurso: int,
    result_data: str,
    result_dezenas: list[int],
    hits_json: dict,
    best_hits: int,
) -> None:
    tz = ZoneInfo(TZ_NAME)
    checked_at = datetime.now(tz)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE predictions
                SET checked = TRUE,
                    checked_at = %s,
                    result_concurso = %s,
                    result_data = %s,
                    result_dezenas = %s,
                    hits_json = %s,
                    best_hits = %s
                WHERE id = %s
                """,
                (
                    checked_at,
                    result_concurso,
                    result_data,
                    Json(result_dezenas),
                    Json(hits_json),
                    best_hits,
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
# FORMATAÇÃO
# =========================
FULLWIDTH_MAP = str.maketrans("0123456789", "０１２３４５６７８９")
SEPARATOR = " • "


def to_fullwidth(value: str) -> str:
    return value.translate(FULLWIDTH_MAP)


def fmt_num(n: int) -> str:
    return to_fullwidth(f"{n:02d}")


def fmt_plain_num(n: int) -> str:
    return f"{n:02d}"


def fmt_nums(nums: list[int]) -> str:
    return SEPARATOR.join(fmt_num(n) for n in nums)


def fmt_nums_multiline(nums: list[int], first_line: int = 7) -> str:
    line1 = SEPARATOR.join(fmt_num(n) for n in nums[:first_line])
    line2 = SEPARATOR.join(fmt_num(n) for n in nums[first_line:])
    return f"{line1}\n{line2}"


# =========================
# MÉTRICAS
# =========================
def _normalize_map(values: dict[int, float], default: float = 0.5) -> dict[int, float]:
    if not values:
        return {}

    vals = list(values.values())
    vmin = min(vals)
    vmax = max(vals)

    if math.isclose(vmin, vmax):
        return {k: default for k in values}

    return {k: (v - vmin) / (vmax - vmin) for k, v in values.items()}


def _build_freq_map(concursos: list[Concurso], window: int) -> dict[int, float]:
    janela = concursos[: min(window, len(concursos))]
    total = max(len(janela), 1)
    freq = Counter()

    for c in janela:
        for n in c.dezenas:
            freq[n] += 1

    return {n: freq[n] / total for n in range(1, 26)}


def _build_recency_map(concursos: list[Concurso], alpha: float = 0.87) -> dict[int, float]:
    scores = {n: 0.0 for n in range(1, 26)}

    for idx, c in enumerate(concursos):
        peso = alpha ** idx
        for n in c.dezenas:
            scores[n] += peso

    return _normalize_map(scores)


def _build_delay_map(concursos: list[Concurso]) -> dict[int, float]:
    atraso = {n: float(len(concursos) + 1) for n in range(1, 26)}

    for idx, c in enumerate(concursos):
        for n in c.dezenas:
            if atraso[n] > len(concursos):
                atraso[n] = float(idx)

    return _normalize_map(atraso)


def _build_zscore_map(concursos: list[Concurso], window: int = 30) -> dict[int, float]:
    freq_map = _build_freq_map(concursos, window)
    values = list(freq_map.values())

    media = statistics.mean(values)
    desvio = statistics.pstdev(values) or 1.0

    zscores = {n: (freq_map[n] - media) / desvio for n in range(1, 26)}
    return _normalize_map(zscores)


def _build_trend_map(concursos: list[Concurso], window: int = 10) -> dict[int, float]:
    janela = list(reversed(concursos[: min(window, len(concursos))]))
    if len(janela) < 2:
        return {n: 0.5 for n in range(1, 26)}

    xs = list(range(len(janela)))
    x_mean = statistics.mean(xs)
    denom = sum((x - x_mean) ** 2 for x in xs) or 1.0

    trend = {}
    for n in range(1, 26):
        ys = [1 if n in c.dezenas else 0 for c in janela]
        y_mean = statistics.mean(ys)
        numer = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
        trend[n] = numer / denom

    return _normalize_map(trend)


def _build_pair_map(concursos: list[Concurso], window: int = 20) -> dict[tuple[int, int], float]:
    janela = concursos[: min(window, len(concursos))]
    pair_count = Counter()

    for c in janela:
        dezenas = sorted(c.dezenas)
        for a, b in combinations(dezenas, 2):
            pair_count[(a, b)] += 1

    if not pair_count:
        return {}

    max_v = max(pair_count.values()) or 1
    return {k: v / max_v for k, v in pair_count.items()}


def _historical_targets(concursos: list[Concurso]) -> tuple[float, float, int]:
    sums = [sum(c.dezenas) for c in concursos[: min(30, len(concursos))]]
    mean_sum = statistics.mean(sums) if sums else 195.0
    std_sum = statistics.pstdev(sums) if len(sums) > 1 else 12.0

    reps = []
    limite = min(len(concursos) - 1, 20)
    for i in range(max(0, limite)):
        a = set(concursos[i].dezenas)
        b = set(concursos[i + 1].dezenas)
        reps.append(len(a & b))

    target_rep = round(statistics.mean(reps)) if reps else 9
    return mean_sum, (std_sum or 12.0), target_rep


def _weighted_unique_sample(
    pool: list[int],
    weights_map: dict[int, float],
    k: int,
    rng: random.Random,
) -> list[int]:
    available = list(pool)
    chosen = []

    while available and len(chosen) < k:
        weights = [max(weights_map.get(n, 0.1), 0.01) for n in available]
        pick = rng.choices(available, weights=weights, k=1)[0]
        chosen.append(pick)
        available.remove(pick)

    return chosen


def _max_consecutive(nums: list[int]) -> int:
    nums = sorted(nums)
    if not nums:
        return 0

    best = 1
    cur = 1

    for a, b in zip(nums, nums[1:]):
        if b == a + 1:
            cur += 1
            best = max(best, cur)
        else:
            cur = 1

    return best


def _row_counts(nums: list[int]) -> list[int]:
    rows = [0, 0, 0, 0, 0]
    for n in nums:
        idx = min((n - 1) // 5, 4)
        rows[idx] += 1
    return rows


def _game_hard_ok(nums: list[int], last_result: list[int], mean_sum: float, std_sum: float) -> bool:
    pares = sum(1 for n in nums if n % 2 == 0)
    baixas = sum(1 for n in nums if n <= 13)
    repetidas = len(set(nums) & set(last_result))
    soma = sum(nums)
    seq = _max_consecutive(nums)

    return (
        5 <= pares <= 10
        and 5 <= baixas <= 10
        and 5 <= repetidas <= 11
        and mean_sum - (2.2 * std_sum) <= soma <= mean_sum + (2.2 * std_sum)
        and seq <= 5
    )


def _game_score(
    nums: list[int],
    dezena_score: dict[int, float],
    pair_map: dict[tuple[int, int], float],
    last_result: list[int],
    mean_sum: float,
    std_sum: float,
    target_rep: int,
) -> float:
    s = sum(dezena_score[n] for n in nums) * 100.0

    pares = sum(1 for n in nums if n % 2 == 0)
    baixas = sum(1 for n in nums if n <= 13)
    soma = sum(nums)
    repetidas = len(set(nums) & set(last_result))
    seq = _max_consecutive(nums)
    rows = _row_counts(nums)

    if 6 <= pares <= 9:
        s += 10
    else:
        s -= abs(7.5 - pares) * 3

    if 6 <= baixas <= 9:
        s += 10
    else:
        s -= abs(7.5 - baixas) * 3

    dist_sum = abs(soma - mean_sum)
    tol_sum = max(std_sum * 1.4, 10)
    s += max(0.0, 12.0 - (dist_sum / tol_sum) * 12.0)

    dist_rep = abs(repetidas - target_rep)
    s += max(0.0, 10.0 - dist_rep * 3.0)

    if seq <= 3:
        s += 7
    elif seq == 4:
        s += 3
    else:
        s -= (seq - 4) * 5

    row_bonus = 0.0
    for c in rows:
        if 1 <= c <= 4:
            row_bonus += 1.5
        elif c == 0 or c >= 5:
            row_bonus -= 2.0
    s += row_bonus

    if pair_map:
        pair_scores = [pair_map.get(tuple(sorted((a, b))), 0.0) for a, b in combinations(nums, 2)]
        s += (sum(pair_scores) / len(pair_scores)) * 12.0

    return s


def _build_profile_weights(
    profile: str,
    base_score: dict[int, float],
    freq5: dict[int, float],
    freq15: dict[int, float],
    trend: dict[int, float],
    delay: dict[int, float],
    zscore: dict[int, float],
) -> dict[int, float]:
    weights = {}

    for n in range(1, 26):
        if profile == "J1":
            value = (
                base_score[n] * 0.70
                + freq5[n] * 0.15
                + freq15[n] * 0.10
                + zscore[n] * 0.05
            )
        elif profile == "J2":
            value = (
                base_score[n] * 0.70
                + trend[n] * 0.10
                + delay[n] * 0.05
                + zscore[n] * 0.15
            )
        elif profile == "J3":
            value = (
                base_score[n] * 0.55
                + trend[n] * 0.25
                + freq5[n] * 0.10
                + zscore[n] * 0.10
            )
        else:
            cold_factor = 1.0 - freq15[n]
            value = (
                base_score[n] * 0.45
                + delay[n] * 0.20
                + trend[n] * 0.10
                + cold_factor * 0.15
                + zscore[n] * 0.10
            )

        weights[n] = max(value, 0.01)

    return weights


def _build_candidate_game(
    profile: str,
    ranking: list[int],
    weights: dict[int, float],
    rng: random.Random,
) -> list[int]:
    top = ranking[:9]
    mid = ranking[9:17]
    low = ranking[17:25]

    if profile == "J1":
        template = (8, 5, 2)
    elif profile == "J2":
        template = (7, 5, 3)
    elif profile == "J3":
        template = (6, 6, 3)
    else:
        template = (5, 6, 4)

    a, b, c = template

    chosen = (
        _weighted_unique_sample(top, weights, a, rng)
        + _weighted_unique_sample(mid, weights, b, rng)
        + _weighted_unique_sample(low, weights, c, rng)
    )

    return sorted(chosen)


def _select_games(
    ranking: list[int],
    base_score: dict[int, float],
    freq5: dict[int, float],
    freq15: dict[int, float],
    trend: dict[int, float],
    delay: dict[int, float],
    zscore: dict[int, float],
    pair_map: dict[tuple[int, int], float],
    last_result: list[int],
    mean_sum: float,
    std_sum: float,
    target_rep: int,
    seed: int,
) -> tuple[list[int], list[int], list[int], list[int]]:
    profiles = ["J1", "J2", "J3", "J4"]
    selected = []

    for idx, profile in enumerate(profiles):
        rng = random.Random(seed + idx * 1000)
        weights = _build_profile_weights(profile, base_score, freq5, freq15, trend, delay, zscore)

        best_game = None
        best_score = -10**9

        for _ in range(1200):
            game = _build_candidate_game(profile, ranking, weights, rng)

            if len(set(game)) != 15:
                continue

            if not _game_hard_ok(game, last_result, mean_sum, std_sum):
                continue

            too_close = False
            for prev in selected:
                if len(set(game) & set(prev)) > 11:
                    too_close = True
                    break
            if too_close:
                continue

            score = _game_score(
                nums=game,
                dezena_score=base_score,
                pair_map=pair_map,
                last_result=last_result,
                mean_sum=mean_sum,
                std_sum=std_sum,
                target_rep=target_rep,
            )

            if score > best_score:
                best_score = score
                best_game = game

        if best_game is None:
            for _ in range(500):
                game = _build_candidate_game(profile, ranking, weights, rng)
                if len(set(game)) != 15:
                    continue

                score = _game_score(
                    nums=game,
                    dezena_score=base_score,
                    pair_map=pair_map,
                    last_result=last_result,
                    mean_sum=mean_sum,
                    std_sum=std_sum,
                    target_rep=target_rep,
                )

                if score > best_score:
                    best_score = score
                    best_game = game

        if best_game is None:
            if profile == "J1":
                best_game = sorted(ranking[:15])
            elif profile == "J2":
                best_game = sorted(ranking[:10] + ranking[15:20])
            elif profile == "J3":
                best_game = sorted(ranking[:8] + ranking[10:17])
            else:
                best_game = sorted(ranking[5:20])

        selected.append(best_game)

    return selected[0], selected[1], selected[2], selected[3]


# =========================
# LÓGICA PRINCIPAL
# =========================
async def build_analysis(lookback: int = 5) -> Analise:
    history_limit = max(lookback, 30)
    raw_concursos = fetch_latest_results(limit=history_limit)

    all_concursos = [
        Concurso(
            numero=int(c["numero"]),
            data=str(c["data"]),
            dezenas=sorted(int(n) for n in c["dezenas"]),
        )
        for c in raw_concursos
    ]

    if not all_concursos:
        raise RuntimeError("Nenhum concurso retornado pela API ou cache.")

    effective_lookback = min(lookback, len(all_concursos))
    concursos_exibicao = all_concursos[:effective_lookback]
    last_result = all_concursos[0].dezenas

    freq5 = _build_freq_map(all_concursos, 5)
    freq15 = _build_freq_map(all_concursos, 15)
    freq30 = _build_freq_map(all_concursos, 30)
    recency = _build_recency_map(all_concursos, alpha=0.87)
    delay = _build_delay_map(all_concursos)
    zscore = _build_zscore_map(all_concursos, window=30)
    trend = _build_trend_map(all_concursos, window=10)
    pair_map = _build_pair_map(all_concursos, window=20)

    dezena_score = {}
    for n in range(1, 26):
        dezena_score[n] = (
            (freq5[n] * 0.30)
            + (freq15[n] * 0.20)
            + (freq30[n] * 0.15)
            + (recency[n] * 0.15)
            + (zscore[n] * 0.10)
            + (delay[n] * 0.05)
            + (trend[n] * 0.05)
        )

    ranking = sorted(range(1, 26), key=lambda n: (-dezena_score[n], n))

    d1 = sorted(ranking[:10])
    d2 = sorted(ranking[10:15])
    d3 = sorted(ranking[15:20])
    d4 = sorted(ranking[20:25])

    mean_sum, std_sum, target_rep = _historical_targets(all_concursos)
    seed = all_concursos[0].numero

    j1, j2, j3, j4 = _select_games(
        ranking=ranking,
        base_score=dezena_score,
        freq5=freq5,
        freq15=freq15,
        trend=trend,
        delay=delay,
        zscore=zscore,
        pair_map=pair_map,
        last_result=last_result,
        mean_sum=mean_sum,
        std_sum=std_sum,
        target_rep=target_rep,
        seed=seed,
    )

    return Analise(
        concursos=concursos_exibicao,
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
# RENDER
# =========================
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


def build_hits_json(games: dict, result_nums: list[int]) -> tuple[dict, int]:
    hits_json = {}
    best_hits = 0

    for name in ("J1", "J2", "J3", "J4"):
        nums = sorted(int(n) for n in games[name])
        acertadas = sorted(set(nums) & set(result_nums))
        hits = len(acertadas)
        best_hits = max(best_hits, hits)

        hits_json[name] = {
            "game": nums,
            "hits": hits,
            "matched_numbers": acertadas,
        }

    return hits_json, best_hits


def render_result_check(
    result_concurso: int,
    result_data: str,
    result_nums: list[int],
    hits_json: dict,
    best_hits: int,
) -> str:
    blocks = []

    for name in ("J1", "J2", "J3", "J4"):
        game_data = hits_json[name]
        nums = game_data["game"]
        hits = game_data["hits"]
        acertadas = game_data["matched_numbers"]

        medal = "🏆" if hits >= 11 else "🎟"
        acertadas_txt = "—" if not acertadas else SEPARATOR.join(fmt_num(n) for n in acertadas)

        blocks.append(
            f"{medal} <b>{name}</b> — <b>{fmt_plain_num(hits)}</b> acertos\n"
            f"{fmt_nums_multiline(nums)}\n"
            f"✅ Acertadas: {acertadas_txt}"
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
        latest_results = fetch_latest_results(limit=30)
    except Exception as e:
        logger.exception("Erro ao buscar resultados para conferência: %s", e)
        return

    result_map = {}
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

        games = prediction["games_json"]
        hits_json, best_hits = build_hits_json(games, found["dezenas"])

        text = render_result_check(
            result_concurso=found["numero"],
            result_data=found["data"],
            result_nums=found["dezenas"],
            hits_json=hits_json,
            best_hits=best_hits,
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
                hits_json=hits_json,
                best_hits=best_hits,
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
        render_analysis(analysis, len(analysis.concursos), target_concurso),
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
