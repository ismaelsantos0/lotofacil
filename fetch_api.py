import requests

API_URL = "https://servicebus2.caixa.gov.br/portaldeloterias/api/lotofacil"

def fetch_latest_results(limit: int = 30) -> list[dict]:
    r = requests.get(API_URL, timeout=30)
    r.raise_for_status()
    data = r.json()

    concursos = data if isinstance(data, list) else [data]
    resultados = []

    for item in concursos[:limit]:
        dezenas = item.get("listaDezenas") or item.get("dezenas") or []
        numero = item.get("numero")
        data_apuracao = item.get("dataApuracao") or item.get("data")

        resultados.append(
            {
                "numero": int(numero),
                "data": str(data_apuracao),
                "dezenas": [int(x) for x in dezenas],
            }
        )

    return resultados
