from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from datetime import date
from io import BytesIO

from pypdf import PdfReader


MAX_PDF_BYTES = 15 * 1024 * 1024
MAX_PDF_PAGES = 100
DATE_PATTERN = re.compile(r"\d{2}/\d{2}/\d{4}")
DECIMAL_TOKEN = re.compile(r"[-+]?\d[\d.,]*")


def _ascii(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()


def _parse_number(value: str) -> float:
    normalized = value.strip().replace(".", "").replace(",", ".")
    try:
        return float(normalized)
    except ValueError as exc:
        raise ValueError("O extrato contém um valor numérico inválido.") from exc


def _parse_date(value: str) -> date:
    day, month, year = value.split("/")
    return date(int(year), int(month), int(day))


def _report_date(text: str) -> date | None:
    normalized = _ascii(text)
    marker = normalized.find("data/hora")
    if marker < 0:
        return None
    match = DATE_PATTERN.search(text[marker:marker + 180])
    return _parse_date(match.group()) if match else None


def _row_name(prefix: str, pending_name: str) -> str:
    prefix = " ".join(prefix.split())
    pending_name = " ".join(pending_name.split())
    if pending_name and prefix:
        return f"{pending_name} {prefix}"
    return pending_name or prefix


def parse_agora_pdf(content: bytes) -> dict:
    """Extract positions from the detailed 'Fundos e clubes' table in an Ágora statement."""
    if not content or len(content) > MAX_PDF_BYTES:
        raise ValueError("O PDF está vazio ou excede o limite de 15 MB.")
    try:
        reader = PdfReader(BytesIO(content), strict=False)
    except Exception as exc:
        raise ValueError("Não foi possível abrir este PDF.") from exc
    if reader.is_encrypted:
        raise ValueError("O PDF está protegido por senha. Envie uma cópia desbloqueada.")
    if len(reader.pages) > MAX_PDF_PAGES:
        raise ValueError("O PDF excede o limite de 100 páginas.")

    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception as exc:
            raise ValueError("Não foi possível ler o texto de uma página do PDF.") from exc
    all_text = "\n".join(pages)
    if not all_text.strip():
        raise ValueError("O PDF não contém texto selecionável para leitura automática.")
    report_date = _report_date(pages[0] if pages else all_text)

    detail_lines: list[str] | None = None
    for page_text in pages:
        if "fundos e clubes de investimentos" in _ascii(page_text):
            detail_lines = page_text.splitlines()
            break
    if detail_lines is None:
        raise ValueError("Não encontrei a tabela detalhada 'Fundos e clubes de investimentos' da Ágora.")

    pending_name = ""
    parsed: list[dict] = []
    for raw_line in detail_lines:
        line = " ".join(raw_line.split())
        if not line:
            continue
        normalized = _ascii(line)
        if normalized.startswith("total"):
            break
        if normalized.startswith(("fundos e clubes", "valores expressos", "data de", "quantidade de", "valor atual da", "valor principal", "aplicado valor", "percentual")):
            continue

        date_match = DATE_PATTERN.search(line)
        if date_match:
            name_prefix = line[:date_match.start()].strip()
            number_tokens = [
                token for token in line[date_match.end():].split()
                if DECIMAL_TOKEN.fullmatch(token)
            ]
            if len(number_tokens) < 5:
                continue
            name = _row_name(name_prefix, pending_name)
            pending_name = ""
            numbers = number_tokens
            reference_date = _parse_date(date_match.group())
        else:
            tokens = line.split()
            suffix_start = len(tokens)
            while suffix_start > 0 and DECIMAL_TOKEN.fullmatch(tokens[suffix_start - 1]):
                suffix_start -= 1
            numeric_tokens = tokens[suffix_start:]
            if len(numeric_tokens) >= 6 and suffix_start > 0:
                name = " ".join(tokens[:suffix_start]).strip()
                numbers = numeric_tokens
                reference_date = None
                pending_name = ""
            else:
                if normalized.startswith("agora "):
                    pending_name = line
                continue

        if not name or len(numbers) < 5:
            continue
        try:
            units = _parse_number(numbers[0])
            unit_price = _parse_number(numbers[1])
            invested_value = _parse_number(numbers[2])
            current_value = _parse_number(numbers[3])
        except ValueError:
            continue
        parsed.append({
            "name": name,
            "brokerage": "Ágora",
            "asset_type": "fundo",
            "units": units,
            "current_unit_price": unit_price,
            "invested_value": invested_value,
            "current_value": current_value,
            "purchase_date": None,
            "reference_date": reference_date,
            "maturity_date": None,
            "tax_note": "",
        })

    if not parsed:
        raise ValueError("Não encontrei posições na tabela de fundos da Ágora.")

    occurrences: defaultdict[str, int] = defaultdict(int)
    for item in parsed:
        slug = re.sub(r"[^a-z0-9]+", "-", _ascii(item["name"])).strip("-") or "ativo"
        occurrences[slug] += 1
        item["source_key"] = f"agora:fundos:{slug}:{occurrences[slug]}"

    return {
        "brokerage": "Ágora",
        "report_date": report_date,
        "items": parsed,
    }
