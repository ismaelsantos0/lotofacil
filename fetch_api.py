import requests

API_URL = "https://servicebus2.caixa.gov.br/portaldeloterias/api/lotofacil"


def _parse_item(item: dict) -> dict:
    dezenas = (
        item.get("listaDezenas")
        or item.get("dezenas")
        or item.get("numerosSorteadosOrdemSorteio")
        or []
    )

    numero = item.get("numero")
    data_apuracao = item.get("dataApuracao") or item.get("data") or ""

    return {
        "numero": int(numero),
        "data": str(data_apuracao),
        "dezenas": [int(x) for x in dezenas],
    }


def fetch_latest_results(limit: int = 30) -> list[dict]:
    resp = requests.get(API_URL, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    # Se a API devolver uma lista
    if isinstance(data, list):
        parsed = [_parse_item(item) for item in data]
        parsed.sort(key=lambda x: x["numero"], reverse=True)
        return parsed[:limit]

    # Se devolver só um concurso
    if isinstance(data, dict):
        return [_parse_item(data)]

    return []
