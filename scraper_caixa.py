from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, List

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

LOTOFACIL_URL = "https://loterias.caixa.gov.br/Paginas/Lotofacil.aspx"


@dataclass
class ConcursoLotofacil:
    numero: int
    data: str
    dezenas: List[int]


class CaixaScraperError(Exception):
    pass


def _norm_dezenas(values: list[Any]) -> list[int]:
    dezenas: list[int] = []
    for v in values:
        s = str(v).strip()
        s = re.sub(r"\D", "", s)
        if not s:
            continue
        n = int(s)
        if 1 <= n <= 25:
            dezenas.append(n)

    if len(dezenas) < 15:
        raise CaixaScraperError(f"Menos de 15 dezenas válidas: {dezenas}")

    return sorted(dezenas[:15])


def _extract_from_obj(obj: Any) -> ConcursoLotofacil | None:
    """
    Tenta reconhecer estruturas JSON comuns da CAIXA/SPA.
    """
    if isinstance(obj, dict):
        numero = obj.get("numero") or obj.get("numeroConcurso")
        data = obj.get("dataApuracao") or obj.get("data")
        dezenas = (
            obj.get("listaDezenas")
            or obj.get("dezenas")
            or obj.get("resultadoOrdenado")
        )

        if numero and data and isinstance(dezenas, list):
            try:
                return ConcursoLotofacil(
                    numero=int(str(numero)),
                    data=str(data).strip(),
                    dezenas=_norm_dezenas(dezenas),
                )
            except Exception:
                pass

        for v in obj.values():
            found = _extract_from_obj(v)
            if found:
                return found

    elif isinstance(obj, list):
        for item in obj:
            found = _extract_from_obj(item)
            if found:
                return found

    return None


async def _extract_current_result_from_page(page) -> ConcursoLotofacil:
    """
    Estratégia:
    1) esperar networkidle
    2) tentar achar JSON embutido no DOM
    3) tentar ler texto visível como fallback
    """
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_load_state("networkidle")

    # 1) tenta capturar JSON de scripts ou conteúdo embutido
    html = await page.content()

    # procura blocos JSON plausíveis no HTML
    possible_json_blocks = re.findall(r"(\{.*?\})", html, re.DOTALL)
    for block in possible_json_blocks:
        if "dataApuracao" not in block and "listaDezenas" not in block and "numeroConcurso" not in block:
            continue
        try:
            data = json.loads(block)
            found = _extract_from_obj(data)
            if found:
                return found
        except Exception:
            continue

    # 2) tenta pegar do texto da página
    body_text = await page.locator("body").inner_text()

    # cabeçalho renderizado
    m_header = re.search(
        r"Resultado\s+Concurso\s+(\d+)\s+\((\d{2}/\d{2}/\d{4})\)",
        body_text,
        re.I,
    )

    dezenas_all = re.findall(r"\b\d{2}\b", body_text)
    dezenas = []
    seen = set()
    for d in dezenas_all:
        n = int(d)
        if 1 <= n <= 25 and n not in seen:
            seen.add(n)
            dezenas.append(n)

    if m_header and len(dezenas) >= 15:
        return ConcursoLotofacil(
            numero=int(m_header.group(1)),
            data=m_header.group(2),
            dezenas=sorted(dezenas[:15]),
        )

    raise CaixaScraperError(
        "A página carregou, mas o resultado não foi materializado nem em JSON embutido nem no texto visível."
    )


async def fetch_latest_results(limit: int = 5) -> List[ConcursoLotofacil]:
    if limit < 1:
        raise ValueError("limit deve ser >= 1")

    results: List[ConcursoLotofacil] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
            ],
        )

        context = await browser.new_context(
            locale="pt-BR",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        await page.goto(LOTOFACIL_URL, wait_until="domcontentloaded", timeout=90000)

        # tenta o concurso atual
        current = await _extract_current_result_from_page(page)
        results.append(current)

        # tenta navegar pelos anteriores
        for _ in range(limit - 1):
            body_before = await page.locator("body").inner_text()

            anterior = page.get_by_text(re.compile(r"Anterior", re.I))

            try:
                await anterior.first.click(timeout=15000)
            except PlaywrightTimeoutError:
                break

            try:
                await page.wait_for_function(
                    """
                    (oldText) => {
                        const now = document.body?.innerText || "";
                        return now !== oldText;
                    }
                    """,
                    arg=body_before,
                    timeout=30000,
                )
            except PlaywrightTimeoutError:
                break

            try:
                item = await _extract_current_result_from_page(page)
            except Exception:
                break

            if any(r.numero == item.numero for r in results):
                break

            results.append(item)

        await context.close()
        await browser.close()

    if not results:
        raise CaixaScraperError("Nenhum resultado foi extraído da CAIXA.")

    return results
