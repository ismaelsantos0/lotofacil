import requests

def fetch_latest_results(limit=5):
    url = "https://loteriascaixa-api.herokuapp.com/api/lotofacil"

    response = requests.get(url, timeout=20)
    data = response.json()

    concursos = []

    for item in data[:limit]:
        concursos.append({
            "numero": int(item["concurso"]),
            "data": item["data"],
            "dezenas": sorted([int(x) for x in item["dezenas"]])
        })

    return concursos
