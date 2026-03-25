from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

LOTOFACIL_URL = "https://loterias.caixa.gov.br/Paginas/Lotofacil.aspx"


@dataclass
class ConcursoLotofacil:
    numero: int
    data: str
    dezenas: List[int]


class CaixaScraperError(Exception):
    pass


def _parse_header(text: str) -> tuple[int, str]:
    m = re.search(r"Resultado\s+Concurso\s+(\d+)\s+\((\d{2}/\d{2}/\d{4})\)", text, re.I)
    if not m:
        raise CaixaScraperError(f"Não consegui interpretar o cabeçalho: {text!r}")
    return int(m.group(1)), m.group(2)


def _extract_dezenas_from_text(text: str) -> List[int]:
    nums = [int(x) for x in re.findall(r"\b\d{1,2}\b", text)]
    dezenas = [n for n in nums if 1 <= n <= 25]

    seen = set()
    ordered = []
    for n in dezenas:
        if n not in seen:
            seen.add(n)
            ordered.append(n)

    if len(ordered) < 15:
        raise CaixaScraperError(f"Menos de 15 dezenas encontradas: {ordered}")

    return sorted(ordered[:15])


def fetch_latest_results(limit: int = 5) -> List[ConcursoLotofacil]:
    if limit < 1:
        raise ValueError("limit deve ser >= 1")

    results: List[ConcursoLotofacil] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
            ],
        )

        page = browser.new_page(locale="pt-BR")
        page.goto(LOTOFACIL_URL, wait_until="domcontentloaded", timeout=60000)

        header_locator = page.get_by_text(
            re.compile(r"Resultado Concurso \d+ \(\d{2}/\d{2}/\d{4}\)")
        )

        try:
            header_locator.first.wait_for(timeout=60000)
        except PlaywrightTimeoutError:
            browser.close()
            raise CaixaScraperError(
                "A página da CAIXA carregou, mas o cabeçalho do resultado não apareceu."
            )

        for i in range(limit):
            header_text = header_locator.first.inner_text().strip()
            numero, data = _parse_header(header_text)

            body_text = page.locator("body").inner_text()
            dezenas = _extract_dezenas_from_text(body_text)

            current = ConcursoLotofacil(numero=numero, data=data, dezenas=dezenas)

            if results and results[-1].numero == current.numero:
                browser.close()
                raise CaixaScraperError(
                    f"O site não avançou para o concurso anterior. Concurso repetido: {current.numero}"
                )

            results.append(current)

            if i < limit - 1:
                anterior_locator = page.get_by_text(re.compile(r"^\s*<\s*Anterior\s*$"))

                try:
                    anterior_locator.first.click(timeout=15000)
                except PlaywrightTimeoutError:
                    browser.close()
                    raise CaixaScraperError("Não consegui clicar em 'Anterior'.")

                try:
                    page.wait_for_function(
                        """
                        (oldHeader) => {
                            const txt = document.body?.innerText || "";
                            return !txt.includes(oldHeader);
                        }
                        """,
                        arg=header_text,
                        timeout=30000,
                    )
                except PlaywrightTimeoutError:
                    browser.close()
                    raise CaixaScraperError(
                        "Cliquei em 'Anterior', mas a página não atualizou para o concurso anterior."
                    )

        browser.close()

    return results
