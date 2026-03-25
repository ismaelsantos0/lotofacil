from scraper_caixa import fetch_latest_results

concursos = fetch_latest_results(limit=5, headless=False)

for c in concursos:
    print(f"Concurso {c.numero} - {c.data}")
    print(" ".join(f"{n:02d}" for n in c.dezenas))
    print("-" * 40)
