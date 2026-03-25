from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import List

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


LOTOfACIL_URL = "https://loterias.caixa.gov.br/Paginas/Lotofacil.aspx"


@dataclass
class ConcursoLotofacil:
    numero: int
    data: str
    dezenas: List[int]


class CaixaScraperError(Exception):
    pass


def _parse_header(text: str) -> tuple[int, str]:
    """
    Exemplo esperado:
    'Resultado Concurso 3387 (24/03/2026)'
    """
    m = re.search(r"Resultado\s+Concurso\s+(\d+)\s+\((\d{2}/\d{2}/\d{4})\)", text, re.I)
    if not m:
        raise CaixaScraperError(f"Não consegui interpretar o cabeçalho: {text!r}")
    return int(m.group(1)), m.group(2)


def _parse_dezenas(text: str) -> List[int]:
    """
    Extrai dezenas do bloco visível.
    Mantém apenas números entre 1 e 25.
    """
    nums = [int(x) for x in re.findall(r"\b\d{1,2}\b", text)]
    dezenas = [n for n in nums if 1 <= n <= 25]

    # remove duplicadas preservando ordem
    seen = set()
    final = []
    for n in dezenas:
        if n not in seen:
            seen.add(n)
            final.append(n)

    if len(final) < 15:
        raise CaixaScraperError(f"Encontrei menos de 15 dezenas: {final}")

    return sorted(final[:15])


def fetch_latest_results(limit: int = 5, headless: bool = True) -> List[ConcursoLotofacil]:
    """
    Abre a página oficial da CAIXA, lê o concurso atual
    e navega clicando em 'Anterior' para montar os últimos concursos.
    """
    if limit < 1:
        raise ValueError("limit deve ser >= 1")

    results: List[ConcursoLotofacil] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        page = browser.new_page(locale="pt-BR")
        page.goto(LOTOfACIL_URL, wait_until="domcontentloaded", timeout=60000)

        # espera o cabeçalho de resultado renderizar
        header = page.get_by_text(re.compile(r"Resultado Concurso \d+ \(\d{2}/\d{2}/\d{4}\)"))
        header.first.wait_for(timeout=60000)

        for i in range(limit):
            header_text = header.first.inner_text().strip()
            numero, data = _parse_header(header_text)

            # pega um bloco próximo do resultado.
            # Como a estrutura pode mudar, usamos um recorte da página.
            page_text = page.locator("body").inner_text()
            dezenas = _parse_dezenas(page_text)

            item = ConcursoLotofacil(numero=numero, data=data, dezenas=dezenas)

            # evita repetição se o clique falhar
            if results and results[-1].numero == item.numero:
                raise CaixaScraperError(
                    f"O site não avançou para o concurso anterior. Concurso repetido: {item.numero}"
                )

            results.append(item)

            if i < limit - 1:
                anterior = page.get_by_text(re.compile(r"^\s*<\s*Anterior\s*$"))
                anterior.first.click(timeout=15000)

                # espera mudar o número do concurso
                page.wait_for_timeout(1500)
                page.wait_for_function(
                    """
                    ([oldText]) => {
                        const body = document.body?.innerText || "";
                        return !body.includes(oldText);
                    }
                    """,
                    arg=[header_text],
                    timeout=30000,
                )

        browser.close()

    return results


if __name__ == "__main__":
    try:
        concursos = fetch_latest_results(limit=5, headless=True)
        for c in concursos:
            print(asdict(c))
    except PlaywrightTimeoutError as e:
        print(f"Timeout ao carregar página da CAIXA: {e}")
    except Exception as e:
        print(f"Erro: {e}")
