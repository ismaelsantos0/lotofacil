import json
import os
from pathlib import Path

import requests

API_URL = "https://servicebus2.caixa.gov.br/portaldeloterias/api/lotofacil"
CACHE_FILE = Path("lotofacil.json")

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "pt-BR,pt;q=0.9,en;q=0.8",
    "origin": "https://loterias.caixa.gov.br",
    "referer": "https://loterias.caixa.gov.br/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
}


def _parse_item(item: dict) -> dict:
    dezenas = (
        item.get("listaDezenas")
        or item.get("dezenas")
        or item.get("numerosSorteadosOrdemSorteio")
        or []
    )

    numero = (
        item.get("numero")
        or item.get("numeroDoConcurso")
        or item.get("concurso")
    )

    data_apuracao = (
        item.get("dataApuracao")
        or item.get("data")
        or item.get("dataPorExtenso")
        or ""
    )

    return {
        "numero": int(numero),
        "data": str(data_apuracao),
        "dezenas": [int(x) for x in dezenas],
    }


def _load_cache(limit: int) -> list[dict]:
    if not CACHE_FILE.exists():
        return []

    with CACHE_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        data = [data]

    parsed = [_parse_item(item) for item in data]
    parsed.sort(key=lambda x: x["numero"], reverse=True)
    return parsed[:limit]


def _save_cache(data: list[dict]) -> None:
    with CACHE_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_latest_results(limit: int = 30) -> list[dict]:
    try:
        resp = requests.get(API_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, dict):
            parsed = [_parse_item(data)]
        elif isinstance(data, list):
            parsed = [_parse_item(item) for item in data]
        else:
            parsed = []

        parsed.sort(key=lambda x: x["numero"], reverse=True)

        # atualiza cache se vier algo válido
        if parsed:
            cache_all = _load_cache(limit=500)
            existing = {item["numero"]: item for item in cache_all}
            for item in parsed:
                existing[item["numero"]] = item
            merged = sorted(existing.values(), key=lambda x: x["numero"], reverse=True)
            _save_cache(merged)

        return parsed[:limit]

    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", None)

        # se tomou 403, tenta cache local
        if status == 403:
            cached = _load_cache(limit)
            if cached:
                return cached
            raise RuntimeError(
                "403 da CAIXA e sem cache local. O IP do Railway provavelmente está bloqueado."
            ) from e

        cached = _load_cache(limit)
        if cached:
            return cached
        raise

    except Exception:
        cached = _load_cache(limit)
        if cached:
            return cached
        raise
