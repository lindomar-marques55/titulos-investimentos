from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import base64
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import bcrypt
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .agora_import import parse_agora_pdf


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
SEED_HOLIDAYS_FILE = APP_DIR / "data_seed" / "holidays.json"
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "portfolio.sqlite3"
COOKIE_NAME = "titulos_session"
SESSION_MAX_AGE = 7 * 24 * 60 * 60
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
TIMEZONE_NAME = os.environ.get("APP_TIMEZONE", "America/Sao_Paulo")
INITIAL_ADMIN_USERNAME = os.environ.get("INITIAL_ADMIN_USERNAME", "admin")
INITIAL_ADMIN_PASSWORD = os.environ.get("INITIAL_ADMIN_PASSWORD", "")
LEGACY_APP_PASSWORD = os.environ.get("LEGACY_APP_PASSWORD", "")
SESSION_SIGNING_KEY: bytes | None = None
FX_RATE_CACHE: dict[str, tuple[float, dict]] = {}
FX_RATE_CACHE_LOCK = threading.Lock()
FX_CENT = Decimal("0.01")
ASSET_TYPE_LABELS = {
    "titulo_publico": "Título público",
    "titulo_privado": "Título privado",
    "cdb": "CDB",
    "lci": "LCI",
    "lca": "LCA",
    "fundo": "Fundo",
    "acao": "Ação",
    "fii": "FII",
    "etf": "ETF",
    "bdr": "BDR",
    "cripto": "Criptomoeda",
    "ouro": "Ouro",
    "coe": "COE",
    "opcao": "Opção",
    "previdencia": "Previdência",
    "outro": "Outro",
}
MIN_PROJECTION_HISTORY_30D = 30
MIN_PROJECTION_HISTORY_360D = 90
MIN_PROJECTION_HISTORY_NEXT_DAY = 5


def get_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(TIMEZONE_NAME)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"Fuso horário inválido: {TIMEZONE_NAME}") from exc


def _fetch_json(url: str) -> dict:
    request = UrlRequest(url, headers={"Accept": "application/json", "User-Agent": "Investimentos/1.5.0"})
    with urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_usd_brl_quote() -> dict:
    today = local_today()
    start = today - timedelta(days=14)
    params = urlencode({
        "@moeda": "'USD'",
        "@dataInicial": f"'{start.strftime('%m-%d-%Y')}'",
        "@dataFinalCotacao": f"'{today.strftime('%m-%d-%Y')}'",
        "$format": "json",
    })
    endpoint = (
        "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
        "CotacaoMoedaPeriodo(moeda=@moeda,dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)"
    )
    data = _fetch_json(f"{endpoint}?{params}")
    records = data.get("value", [])
    if not records:
        raise ValueError("O Banco Central não retornou uma cotação USD/BRL recente.")
    closings = [row for row in records if str(row.get("tipoBoletim", "")).casefold() == "fechamento"]
    selected = max(closings or records, key=lambda row: str(row.get("dataHoraCotacao", "")))
    rate = Decimal(str(selected.get("cotacaoVenda", "0")))
    if rate <= 0:
        raise ValueError("A cotação de venda USD/BRL retornada pelo Banco Central é inválida.")
    return {
        "currency": "USD",
        "rate": rate,
        "source": "Banco Central do Brasil — PTAX, cotação de venda",
        "quote_time": selected.get("dataHoraCotacao"),
        "fetched_at": datetime.now(get_timezone()).isoformat(timespec="seconds"),
    }


def _fetch_btc_brl_quote() -> dict:
    data = _fetch_json("https://api.coinbase.com/v2/prices/BTC-BRL/spot")
    amount = data.get("data", {}).get("amount")
    rate = Decimal(str(amount or "0"))
    if rate <= 0:
        raise ValueError("A Coinbase não retornou uma cotação BTC/BRL válida.")
    return {
        "currency": "BTC",
        "rate": rate,
        "source": "Coinbase — preço spot BTC/BRL",
        "quote_time": datetime.now(get_timezone()).isoformat(timespec="seconds"),
        "fetched_at": datetime.now(get_timezone()).isoformat(timespec="seconds"),
    }


def get_fx_quote(currency: str) -> dict:
    if currency == "BRL":
        now = datetime.now(get_timezone()).isoformat(timespec="seconds")
        return {"currency": "BRL", "rate": Decimal("1"), "source": "Real brasileiro", "quote_time": now, "fetched_at": now}
    if currency not in {"USD", "BTC"}:
        raise ValueError("Moeda não suportada. Selecione BRL, USD ou BTC.")

    now = time.monotonic()
    with FX_RATE_CACHE_LOCK:
        cached = FX_RATE_CACHE.get(currency)
        if cached and cached[0] > now:
            return cached[1]
    quote = _fetch_usd_brl_quote() if currency == "USD" else _fetch_btc_brl_quote()
    ttl = 900 if currency == "USD" else 30
    with FX_RATE_CACHE_LOCK:
        FX_RATE_CACHE[currency] = (time.monotonic() + ttl, quote)
    return quote


def public_fx_quote(quote: dict) -> dict:
    return {
        "currency": quote["currency"],
        "brl_per_unit": float(quote["rate"]),
        "source": quote["source"],
        "quote_time": quote["quote_time"],
        "fetched_at": quote["fetched_at"],
    }


def convert_to_brl(value: Decimal | None, quote: dict, *, cents: bool = False) -> Decimal | None:
    if value is None:
        return None
    converted = value * quote["rate"]
    return converted.quantize(FX_CENT, rounding=ROUND_HALF_UP) if cents else converted


def contribution_in_brl(
    item: "ContributionInput", *, convert_invested_value: bool = True
) -> tuple["ContributionInput", dict]:
    quote = get_fx_quote(item.currency)
    current_value = convert_to_brl(item.current_value, quote, cents=True)
    units = Decimal(str(item.units))
    unit_price = (
        current_value / units
        if current_value is not None and units > 0
        else convert_to_brl(item.current_unit_price, quote)
    )
    converted = item.model_copy(update={
        "invested_value": (
            convert_to_brl(item.invested_value, quote, cents=True)
            if convert_invested_value else item.invested_value
        ),
        "current_value": current_value,
        "current_unit_price": unit_price,
        "currency": "BRL",
    })
    return converted, quote


def connect_db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def database():
    connection = connect_db()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def migrate_holdings_schema(connection: sqlite3.Connection) -> None:
    columns = {row["name"]: row for row in connection.execute("PRAGMA table_info(holdings)")}
    required = {
        "brokerage", "asset_type", "current_unit_price", "reference_date", "source_key"
    }
    purchase_date_required = bool(columns.get("purchase_date") and columns["purchase_date"]["notnull"])
    if required.issubset(columns) and not purchase_date_required:
        return

    connection.execute(
        """CREATE TABLE holdings_migration (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            units REAL NOT NULL DEFAULT 0,
            invested_cents INTEGER NOT NULL DEFAULT 0,
            current_cents INTEGER NOT NULL DEFAULT 0,
            purchase_date TEXT,
            maturity_date TEXT,
            tax_note TEXT NOT NULL DEFAULT '',
            brokerage TEXT NOT NULL DEFAULT 'Não informada',
            asset_type TEXT NOT NULL DEFAULT 'titulo_publico',
            current_unit_price REAL,
            reference_date TEXT,
            source_key TEXT UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    source_columns = set(columns)
    defaults = {
        "brokerage": "'Não informada'",
        "asset_type": "'titulo_publico'",
        "current_unit_price": "NULL",
        "reference_date": "NULL",
        "source_key": "NULL",
    }
    target_columns = [
        "id", "name", "units", "invested_cents", "current_cents", "purchase_date",
        "maturity_date", "tax_note", "brokerage", "asset_type", "current_unit_price",
        "reference_date", "source_key", "created_at", "updated_at",
    ]
    expressions = [
        column if column in source_columns else defaults[column]
        for column in target_columns
    ]
    connection.execute(
        f"INSERT INTO holdings_migration ({', '.join(target_columns)}) "
        f"SELECT {', '.join(expressions)} FROM holdings"
    )
    connection.execute("DROP TABLE holdings")
    connection.execute("ALTER TABLE holdings_migration RENAME TO holdings")


def migrate_contributions_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS contributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            holding_id INTEGER NOT NULL REFERENCES holdings(id) ON DELETE CASCADE,
            units REAL NOT NULL DEFAULT 0,
            invested_cents INTEGER NOT NULL DEFAULT 0,
            current_cents INTEGER NOT NULL DEFAULT 0,
            current_unit_price REAL,
            purchase_date TEXT,
            maturity_date TEXT,
            reference_date TEXT,
            tax_note TEXT NOT NULL DEFAULT '',
            source_key TEXT UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    contribution_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(contributions)")
    }
    maturity_column_added = "maturity_date" not in contribution_columns
    if maturity_column_added:
        connection.execute("ALTER TABLE contributions ADD COLUMN maturity_date TEXT")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS contributions_holding_id ON contributions(holding_id, purchase_date, id)"
    )
    migrated = connection.execute(
        "SELECT 1 FROM app_meta WHERE key = 'contributions_migration_v1'"
    ).fetchone()
    if migrated:
        if maturity_column_added:
            connection.execute(
                """UPDATE contributions SET maturity_date = (
                       SELECT h.maturity_date FROM holdings h WHERE h.id = contributions.holding_id
                   ) WHERE maturity_date IS NULL"""
            )
            connection.execute(
                """INSERT INTO contributions
                   (holding_id, units, invested_cents, current_cents, current_unit_price,
                    purchase_date, maturity_date, reference_date, tax_note, source_key, created_at, updated_at)
                   SELECT h.id, h.units, h.invested_cents, h.current_cents, h.current_unit_price,
                          h.purchase_date, h.maturity_date, h.reference_date, h.tax_note, NULL, h.created_at, h.updated_at
                   FROM holdings h
                   WHERE h.maturity_date IS NOT NULL
                     AND NOT EXISTS (SELECT 1 FROM contributions c WHERE c.holding_id = h.id)"""
            )
            connection.execute("UPDATE holdings SET maturity_date = NULL WHERE maturity_date IS NOT NULL")
        return

    holdings = connection.execute("SELECT * FROM holdings ORDER BY id").fetchall()
    groups: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
    for row in holdings:
        connection.execute(
            """INSERT INTO contributions
               (holding_id, units, invested_cents, current_cents, current_unit_price,
                purchase_date, maturity_date, reference_date, tax_note, source_key, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["id"], row["units"], row["invested_cents"], row["current_cents"],
              row["current_unit_price"], row["purchase_date"], row["maturity_date"], row["reference_date"],
             row["tax_note"], row["source_key"], row["created_at"], row["updated_at"]),
        )
        key = (row["brokerage"].strip().casefold(), row["asset_type"], row["name"].strip().casefold())
        groups.setdefault(key, []).append(row)

    for group in groups.values():
        primary_id = int(group[0]["id"])
        for duplicate in group[1:]:
            connection.execute(
                "UPDATE contributions SET holding_id = ? WHERE holding_id = ?",
                (primary_id, int(duplicate["id"])),
            )
            connection.execute("DELETE FROM holdings WHERE id = ?", (int(duplicate["id"]),))
        rows = connection.execute(
            "SELECT units, invested_cents, current_cents, purchase_date, reference_date FROM contributions WHERE holding_id = ?",
            (primary_id,),
        ).fetchall()
        units = sum(float(item["units"]) for item in rows)
        invested = sum(int(item["invested_cents"]) for item in rows)
        current = sum(int(item["current_cents"]) for item in rows)
        dates = [date.fromisoformat(item["purchase_date"]) for item in rows if item["purchase_date"]]
        average_date = date.fromordinal(int(sum(value.toordinal() for value in dates) / len(dates))).isoformat() if dates else None
        reference_dates = [item["reference_date"] for item in rows if item["reference_date"]]
        reference_date = max(reference_dates) if reference_dates else None
        unit_price = current / 100 / units if units > 0 else None
        connection.execute(
            """UPDATE holdings SET units = ?, invested_cents = ?, current_cents = ?,
                      current_unit_price = ?, purchase_date = ?, reference_date = ?, source_key = NULL
               WHERE id = ?""",
            (units, invested, current, unit_price, average_date, reference_date, primary_id),
        )
    connection.execute("INSERT INTO app_meta (key, value) VALUES ('contributions_migration_v1', 'done')")
    connection.execute("UPDATE holdings SET maturity_date = NULL WHERE maturity_date IS NOT NULL")


def normalize_username(value: str) -> str:
    username = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{3,32}", username):
        raise ValueError("O usuário deve ter de 3 a 32 caracteres: letras, números, ponto, hífen ou sublinhado.")
    return username


def password_is_valid(value: str) -> bool:
    return len(value) >= 12 and len(value.encode("utf-8")) <= 72


def initialize_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    get_timezone()

    with database() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS holdings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                units REAL NOT NULL DEFAULT 0,
                invested_cents INTEGER NOT NULL DEFAULT 0,
                current_cents INTEGER NOT NULL DEFAULT 0,
                purchase_date TEXT,
                maturity_date TEXT,
                tax_note TEXT NOT NULL DEFAULT '',
                brokerage TEXT NOT NULL DEFAULT 'Não informada',
                asset_type TEXT NOT NULL DEFAULT 'titulo_publico',
                current_unit_price REAL,
                reference_date TEXT,
                source_key TEXT UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS holidays (
                holiday_date TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
                active INTEGER NOT NULL DEFAULT 1,
                must_change_password INTEGER NOT NULL DEFAULT 0,
                token_version INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_permissions (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                can_view_portfolio INTEGER NOT NULL DEFAULT 1,
                can_edit_positions INTEGER NOT NULL DEFAULT 0,
                can_manage_calendar INTEGER NOT NULL DEFAULT 0,
                can_manage_backups INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        migrate_holdings_schema(connection)
        migrate_contributions_schema(connection)
        signing_key = connection.execute(
            "SELECT value FROM app_meta WHERE key = 'session_signing_key_v1'"
        ).fetchone()
        if not signing_key:
            connection.execute(
                "INSERT INTO app_meta (key, value) VALUES ('session_signing_key_v1', ?)",
                (secrets.token_hex(32),),
            )
            signing_key = connection.execute(
                "SELECT value FROM app_meta WHERE key = 'session_signing_key_v1'"
            ).fetchone()
        global SESSION_SIGNING_KEY
        SESSION_SIGNING_KEY = bytes.fromhex(signing_key["value"])

        user_count = connection.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if user_count == 0:
            initial_password = INITIAL_ADMIN_PASSWORD or LEGACY_APP_PASSWORD
            if not password_is_valid(initial_password):
                raise RuntimeError(
                    "Banco sem usuários: defina INITIAL_ADMIN_USERNAME e "
                    "INITIAL_ADMIN_PASSWORD (12 a 72 bytes) para criar o administrador inicial."
                )
            try:
                initial_username = normalize_username(INITIAL_ADMIN_USERNAME or "admin")
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc
            now = datetime.now(get_timezone()).isoformat(timespec="seconds")
            result = connection.execute(
                """INSERT INTO users
                   (username, name, password_hash, role, active, must_change_password, token_version, created_at)
                   VALUES (?, ?, ?, 'admin', 1, 1, 0, ?)""",
                (
                    initial_username,
                    "Administrador",
                    bcrypt.hashpw(initial_password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii"),
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO user_permissions
                   (user_id, can_view_portfolio, can_edit_positions, can_manage_calendar, can_manage_backups)
                   VALUES (?, 1, 1, 1, 1)""",
                (result.lastrowid,),
            )
        else:
            users_without_permissions = connection.execute(
                """SELECT u.id, u.role FROM users u
                   LEFT JOIN user_permissions p ON p.user_id = u.id
                   WHERE p.user_id IS NULL"""
            ).fetchall()
            for user in users_without_permissions:
                is_admin = user["role"] == "admin"
                connection.execute(
                    """INSERT INTO user_permissions
                       (user_id, can_view_portfolio, can_edit_positions, can_manage_calendar, can_manage_backups)
                       VALUES (?, ?, ?, ?, ?)""",
                    (user["id"], 1, int(is_admin), int(is_admin), int(is_admin)),
                )

        seeded = connection.execute(
            "SELECT value FROM app_meta WHERE key = 'holiday_seed_v1'"
        ).fetchone()
        if not seeded:
            seed_items = json.loads(SEED_HOLIDAYS_FILE.read_text(encoding="utf-8"))
            connection.executemany(
                "INSERT OR IGNORE INTO holidays (holiday_date, name) VALUES (?, ?)",
                [(item["date"], item["name"]) for item in seed_items],
            )
            connection.execute(
                "INSERT INTO app_meta (key, value) VALUES ('holiday_seed_v1', 'loaded')"
            )


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(
    title="Investimentos — carteira e projeções",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class LoginPayload(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=512)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        return normalize_username(value)


class UserPermissions(BaseModel):
    can_view_portfolio: bool = True
    can_edit_positions: bool = False
    can_manage_calendar: bool = False
    can_manage_backups: bool = False


class AdminUserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=12, max_length=128)
    permissions: UserPermissions = Field(default_factory=UserPermissions)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        return normalize_username(value)

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Informe o nome da pessoa.")
        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if not password_is_valid(value):
            raise ValueError("A senha deve ter pelo menos 12 caracteres e no máximo 72 bytes.")
        return value


class AdminUserUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    password: str | None = Field(default=None, max_length=128)
    active: bool | None = None
    permissions: UserPermissions | None = None

    @field_validator("name")
    @classmethod
    def trim_optional_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("O nome não pode ficar vazio.")
        return value

    @field_validator("password")
    @classmethod
    def validate_optional_password(cls, value: str | None) -> str | None:
        if value is not None and value != "" and not password_is_valid(value):
            raise ValueError("A senha deve ter pelo menos 12 caracteres e no máximo 72 bytes.")
        return value


class PasswordChangePayload(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=12, max_length=128)

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, value: str) -> str:
        if not password_is_valid(value):
            raise ValueError("A nova senha deve ter pelo menos 12 caracteres e no máximo 72 bytes.")
        return value


class HoldingInput(BaseModel):
    name: str = Field(min_length=1, max_length=140)
    brokerage: str = Field(default="Não informada", min_length=1, max_length=100)
    asset_type: Literal[
        "titulo_publico", "titulo_privado", "cdb", "lci", "lca", "fundo", "acao", "fii", "etf", "bdr",
        "cripto", "ouro", "coe", "opcao", "previdencia", "outro",
    ] = "titulo_publico"
    units: Decimal = Field(default=Decimal(0), ge=0, le=Decimal("1000000000"))
    invested_value: Decimal = Field(default=Decimal(0), ge=0, le=Decimal("1000000000000"))
    current_value: Decimal = Field(default=Decimal(0), ge=0, le=Decimal("1000000000000"))
    current_unit_price: Decimal | None = Field(default=None, ge=0, le=Decimal("1000000000000"))
    purchase_date: date | None = None
    reference_date: date | None = None
    maturity_date: date | None = None
    tax_note: str = Field(default="", max_length=140)

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Informe o nome do investimento.")
        return value

    @field_validator("brokerage")
    @classmethod
    def trim_brokerage(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Informe a corretora.")
        return value

    @field_validator("tax_note")
    @classmethod
    def trim_tax_note(cls, value: str) -> str:
        return value.strip()


class ContributionInput(BaseModel):
    units: Decimal = Field(ge=0, le=Decimal("1000000000"))
    invested_value: Decimal = Field(ge=0, le=Decimal("1000000000000"))
    current_value: Decimal = Field(ge=0, le=Decimal("1000000000000"))
    current_unit_price: Decimal | None = Field(default=None, ge=0, le=Decimal("1000000000000"))
    currency: Literal["BRL", "USD", "BTC"] = "BRL"
    purchase_date: date | None = None
    maturity_date: date | None = None
    reference_date: date | None = None
    tax_note: str = Field(default="", max_length=140)

    @field_validator("tax_note")
    @classmethod
    def trim_contribution_tax_note(cls, value: str) -> str:
        return value.strip()


class HolidayInput(BaseModel):
    date: date
    name: str = Field(min_length=1, max_length=140)

    @field_validator("name")
    @classmethod
    def trim_holiday_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Informe o nome do feriado.")
        return value


class QuoteUpdate(BaseModel):
    id: int = Field(ge=1)
    current_value: Decimal = Field(ge=0, le=Decimal("1000000000000"))
    currency: Literal["BRL", "USD", "BTC"] = "BRL"


class QuoteUpdates(BaseModel):
    items: list[QuoteUpdate] = Field(min_length=1, max_length=1000)


class IndividualValueUpdate(BaseModel):
    holding_id: int = Field(ge=1)
    contribution_id: int | None = Field(default=None, ge=1)
    current_value: Decimal = Field(ge=0, le=Decimal("1000000000000"))
    currency: Literal["BRL", "USD", "BTC"] = "BRL"


class IndividualValueUpdates(BaseModel):
    items: list[IndividualValueUpdate] = Field(min_length=1, max_length=1000)


class BackupContribution(ContributionInput):
    currency: Literal["BRL"] = "BRL"
    id: int | None = Field(default=None, ge=1)
    source_key: str | None = Field(default=None, max_length=180)


class BackupHolding(HoldingInput):
    id: int | None = Field(default=None, ge=1)
    source_key: str | None = Field(default=None, max_length=180)
    contributions: list[BackupContribution] = Field(default_factory=list)


class AgoraImportItem(ContributionInput):
    currency: Literal["BRL"] = "BRL"
    name: str = Field(min_length=1, max_length=140)
    brokerage: str = Field(default="Ágora", min_length=1, max_length=100)
    asset_type: Literal[
        "titulo_publico", "titulo_privado", "fundo", "acao", "fii", "etf", "bdr",
        "cripto", "ouro", "coe", "opcao", "previdencia", "outro",
    ] = "fundo"
    source_key: str = Field(min_length=1, max_length=180)

    @field_validator("name", "brokerage")
    @classmethod
    def trim_import_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Nome e corretora são obrigatórios na importação.")
        return value

    @field_validator("source_key")
    @classmethod
    def validate_source_key(cls, value: str) -> str:
        if not value.startswith("agora:fundos:"):
            raise ValueError("Chave de origem inválida para o extrato Ágora.")
        return value


class AgoraImportCommit(BaseModel):
    items: list[AgoraImportItem] = Field(min_length=1, max_length=500)


class BackupHoliday(HolidayInput):
    pass


class BackupPayload(BaseModel):
    format_version: int = Field(ge=1, le=1)
    exported_at: str | None = None
    holdings: list[BackupHolding]
    holidays: list[BackupHoliday]


def make_user_session_token(user: sqlite3.Row | dict) -> str:
    if SESSION_SIGNING_KEY is None:
        raise RuntimeError("Chave de sessão não inicializada.")
    payload = {
        "uid": int(user["id"]),
        "ver": int(user["token_version"]),
        "exp": int(time.time()) + SESSION_MAX_AGE,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    signature = hmac.new(SESSION_SIGNING_KEY, encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def session_user(request: Request) -> dict | None:
    if SESSION_SIGNING_KEY is None:
        return None
    token = request.cookies.get(COOKIE_NAME, "")
    try:
        encoded, signature = token.split(".", 1)
        encoded_bytes = encoded.encode("ascii")
        signature_bytes = signature.encode("ascii")
        expected = hmac.new(SESSION_SIGNING_KEY, encoded_bytes, hashlib.sha256).hexdigest().encode("ascii")
        if not hmac.compare_digest(signature_bytes, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        user_id = int(payload["uid"])
        token_version = int(payload["ver"])
    except (UnicodeEncodeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None

    with database() as connection:
        row = connection.execute(
            """SELECT u.*, p.can_view_portfolio, p.can_edit_positions,
                      p.can_manage_calendar, p.can_manage_backups
               FROM users u LEFT JOIN user_permissions p ON p.user_id = u.id
               WHERE u.id = ?""",
            (user_id,),
        ).fetchone()
    if not row or not row["active"] or int(row["token_version"]) != token_version:
        return None
    user = dict(row)
    if user["can_view_portfolio"] is None:
        user.update({"can_view_portfolio": 1, "can_edit_positions": 0,
                     "can_manage_calendar": 0, "can_manage_backups": 0})
    return user


def has_valid_session(request: Request) -> bool:
    return session_user(request) is not None


def current_user(request: Request) -> dict:
    user = session_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Sessão expirada. Entre novamente.")
    if user["must_change_password"] and request.url.path not in {
        "/api/auth/me", "/api/auth/change-password", "/api/auth/logout"
    }:
        raise HTTPException(status_code=403, detail="Troca de senha obrigatória.", headers={"X-Password-Change-Required": "1"})
    return user


def require_permission(permission: str):
    def dependency(request: Request) -> dict:
        user = current_user(request)
        if user["role"] != "admin" and not user.get(permission):
            raise HTTPException(status_code=403, detail="Seu usuário não tem permissão para esta ação.")
        return user
    return dependency


def require_any_permission(*permissions: str):
    def dependency(request: Request) -> dict:
        user = current_user(request)
        if user["role"] != "admin" and not any(user.get(permission) for permission in permissions):
            raise HTTPException(status_code=403, detail="Seu usuário não tem permissão para esta ação.")
        return user
    return dependency


def require_admin(request: Request) -> dict:
    user = current_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Apenas administradores podem gerenciar usuários.")
    return user


def public_user(user: dict) -> dict:
    return {
        "id": int(user["id"]), "username": user["username"], "name": user["name"],
        "role": user["role"], "active": bool(user["active"]),
        "must_change_password": bool(user["must_change_password"]),
        "permissions": {
            "can_view_portfolio": bool(user.get("can_view_portfolio", 1)),
            "can_edit_positions": bool(user.get("can_edit_positions", 0)),
            "can_manage_calendar": bool(user.get("can_manage_calendar", 0)),
            "can_manage_backups": bool(user.get("can_manage_backups", 0)),
        },
    }


def fetch_user(connection: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT u.*, p.can_view_portfolio, p.can_edit_positions,
                  p.can_manage_calendar, p.can_manage_backups
           FROM users u LEFT JOIN user_permissions p ON p.user_id = u.id WHERE u.id = ?""",
        (user_id,),
    ).fetchone()


def fetch_user_by_username(connection: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT u.*, p.can_view_portfolio, p.can_edit_positions,
                  p.can_manage_calendar, p.can_manage_backups
           FROM users u LEFT JOIN user_permissions p ON p.user_id = u.id
           WHERE u.username = ? COLLATE NOCASE""",
        (username,),
    ).fetchone()


def local_today() -> date:
    return datetime.now(get_timezone()).date()


def to_cents(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def money(cents: int) -> float:
    return round(cents / 100, 2)


def business_days(start: date, end: date, holiday_dates: set[date]) -> int:
    """Conte dias úteis inclusivamente, como NETWORKDAYS do Excel."""
    if end < start:
        return 0
    day = start
    total = 0
    while day <= end:
        if day.weekday() < 5 and day not in holiday_dates:
            total += 1
        day += timedelta(days=1)
    return total


def calendar_days(start: date, end: date) -> int:
    return max(0, (end - start).days + 1)


def projection_days(start: date, end: date, holiday_dates: set[date], asset_type: str) -> int:
    if asset_type == "cripto":
        return calendar_days(start, end)
    return business_days(start, end, holiday_dates)


def read_holiday_dates(connection: sqlite3.Connection) -> set[date]:
    rows = connection.execute("SELECT holiday_date FROM holidays").fetchall()
    return {date.fromisoformat(row["holiday_date"]) for row in rows}


def row_to_holding(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "brokerage": row["brokerage"],
        "asset_type": row["asset_type"],
        "asset_type_label": ASSET_TYPE_LABELS.get(row["asset_type"], "Outro"),
        "units": row["units"],
        "invested_value": money(row["invested_cents"]),
        "current_value": money(row["current_cents"]),
        "current_unit_price": row["current_unit_price"],
        "purchase_date": row["purchase_date"],
        "reference_date": row["reference_date"],
        "maturity_date": row["maturity_date"],
        "tax_note": row["tax_note"],
        "contribution_count": int(row["contribution_count"]) if "contribution_count" in row.keys() else 0,
    }


def list_holding_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT h.*, COUNT(c.id) AS contribution_count
           FROM holdings h LEFT JOIN contributions c ON c.holding_id = h.id
           GROUP BY h.id
           ORDER BY h.brokerage COLLATE NOCASE, h.asset_type, h.name COLLATE NOCASE, h.id"""
    ).fetchall()


def fetch_holding_row(connection: sqlite3.Connection, holding_id: int) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT h.*, COUNT(c.id) AS contribution_count
           FROM holdings h LEFT JOIN contributions c ON c.holding_id = h.id
           WHERE h.id = ? GROUP BY h.id""",
        (holding_id,),
    ).fetchone()


def list_contribution_rows(connection: sqlite3.Connection, holding_id: int) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM contributions WHERE holding_id = ? ORDER BY purchase_date, id",
        (holding_id,),
    ).fetchall()


def contribution_projection(
    contribution: sqlite3.Row,
    holding: sqlite3.Row,
    as_of: date,
    holiday_dates: set[date],
) -> dict:
    invested_cents = int(contribution["invested_cents"])
    current_cents = int(contribution["current_cents"])
    units = float(contribution["units"])
    invested = invested_cents / 100
    current = current_cents / 100
    asset_type = holding["asset_type"]
    basis = "calendar" if asset_type == "cripto" else "business"
    contribution_as_of = date.fromisoformat(contribution["reference_date"]) if contribution["reference_date"] else as_of
    purchase_date = date.fromisoformat(contribution["purchase_date"]) if contribution["purchase_date"] else None
    elapsed = (
        projection_days(purchase_date + timedelta(days=1), contribution_as_of - timedelta(days=1), holiday_dates, asset_type)
        if purchase_date else 0
    )
    days_30 = projection_days(contribution_as_of, contribution_as_of + timedelta(days=30), holiday_dates, asset_type)
    days_360 = projection_days(contribution_as_of, contribution_as_of + timedelta(days=360), holiday_dates, asset_type)
    return_pct = (current / invested - 1) if invested > 0 and units > 0 else None
    daily_rate = None
    next_day = month_value = year_value = None
    month_pct = year_pct_invested = year_pct_current = None
    if return_pct is not None and elapsed > 0:
        daily_rate = (current / invested) ** (1 / elapsed) - 1
        if elapsed >= MIN_PROJECTION_HISTORY_NEXT_DAY:
            next_day = current * (1 + daily_rate)
        if elapsed >= MIN_PROJECTION_HISTORY_30D:
            month_value = current * ((1 + daily_rate) ** days_30)
            month_pct = (month_value / current - 1) if current > 0 else None
        if elapsed >= MIN_PROJECTION_HISTORY_360D:
            year_value = current * ((1 + daily_rate) ** days_360)
            year_pct_invested = (year_value / invested - 1) if invested > 0 else None
            year_pct_current = (year_value / current - 1) if current > 0 else None
    return {
        "id": contribution["id"],
        "units": units,
        "invested_value": money(invested_cents),
        "current_value": money(current_cents),
        "current_unit_price": contribution["current_unit_price"],
        "purchase_date": contribution["purchase_date"],
        "maturity_date": contribution["maturity_date"],
        "reference_date": contribution["reference_date"],
        "as_of": contribution_as_of.isoformat(),
        "tax_note": contribution["tax_note"],
        "return_pct": return_pct,
        "daily_rate": daily_rate,
        "elapsed_days": elapsed,
        "projected_next_day_value": round(next_day, 2) if next_day is not None else None,
        "projected_30d_value": round(month_value, 2) if month_value is not None else None,
        "projected_30d_return_pct": month_pct,
        "projected_360d_value": round(year_value, 2) if year_value is not None else None,
        "projected_360d_return_invested_pct": year_pct_invested,
        "projected_360d_return_current_pct": year_pct_current,
    }


def position_projection(
    holding: sqlite3.Row,
    as_of: date,
    holiday_dates: set[date],
    _: int | None = None,
    __: int | None = None,
) -> dict:
    invested_cents = int(holding["invested_cents"])
    current_cents = int(holding["current_cents"])
    units = float(holding["units"])
    asset_type = holding["asset_type"]
    basis = "calendar" if asset_type == "cripto" else "business"
    invested = invested_cents / 100
    current = current_cents / 100
    holding_as_of = date.fromisoformat(holding["reference_date"]) if holding["reference_date"] else as_of
    purchase_date = date.fromisoformat(holding["purchase_date"]) if holding["purchase_date"] else None
    elapsed = (
        projection_days(purchase_date + timedelta(days=1), holding_as_of - timedelta(days=1), holiday_dates, asset_type)
        if purchase_date else 0
    )
    days_30 = projection_days(holding_as_of, holding_as_of + timedelta(days=30), holiday_dates, asset_type)
    days_360 = projection_days(holding_as_of, holding_as_of + timedelta(days=360), holiday_dates, asset_type)

    return_pct = (current / invested - 1) if invested > 0 and units > 0 else None
    daily_rate = None
    next_day = month_value = year_value = None
    month_pct = year_pct_invested = year_pct_current = None
    if return_pct is not None and elapsed > 0:
        # Converte o retorno acumulado em taxa geométrica diária.
        daily_rate = (current / invested) ** (1 / elapsed) - 1
        if elapsed >= MIN_PROJECTION_HISTORY_NEXT_DAY:
            next_day = current * (1 + daily_rate)
        if elapsed >= MIN_PROJECTION_HISTORY_30D:
            month_value = current * ((1 + daily_rate) ** days_30)
            month_pct = (month_value / current - 1) if current > 0 else None
        if elapsed >= MIN_PROJECTION_HISTORY_360D:
            year_value = current * ((1 + daily_rate) ** days_360)
            year_pct_invested = (year_value / invested - 1) if invested > 0 else None
            year_pct_current = (year_value / current - 1) if current > 0 else None

    return {
        "id": holding["id"],
        "name": holding["name"],
        "brokerage": holding["brokerage"],
        "asset_type": asset_type,
        "asset_type_label": ASSET_TYPE_LABELS.get(asset_type, "Outro"),
        "units": units,
        "invested_value": money(invested_cents),
        "current_value": money(current_cents),
        "current_unit_price": holding["current_unit_price"],
        "purchase_date": holding["purchase_date"],
        "reference_date": holding["reference_date"],
        "as_of": holding_as_of.isoformat(),
        "maturity_date": holding["maturity_date"],
        "tax_note": holding["tax_note"],
        "contribution_count": int(holding["contribution_count"]) if "contribution_count" in holding.keys() else 0,
        "projection_basis": basis,
        "elapsed_days": elapsed,
        "return_pct": return_pct,
        "daily_rate": daily_rate,
        "projected_next_day_value": round(next_day, 2) if next_day is not None else None,
        "projected_30d_value": round(month_value, 2) if month_value is not None else None,
        "projected_30d_return_pct": month_pct,
        "projected_360d_value": round(year_value, 2) if year_value is not None else None,
        "projected_360d_return_invested_pct": year_pct_invested,
        "projected_360d_return_current_pct": year_pct_current,
    }


def create_holding(
    connection: sqlite3.Connection,
    item: HoldingInput,
    item_id: int | None = None,
    source_key: str | None = None,
) -> int:
    now = datetime.now(get_timezone()).isoformat(timespec="seconds")
    if item_id is None:
        cursor = connection.execute(
            """INSERT INTO holdings
               (name, brokerage, asset_type, units, invested_cents, current_cents,
                current_unit_price, purchase_date, reference_date, maturity_date,
                tax_note, source_key, created_at, updated_at)
               VALUES (?, ?, ?, 0, 0, 0, NULL, NULL, NULL, NULL, ?, NULL, ?, ?)""",
            (item.name, item.brokerage, item.asset_type, item.tax_note, now, now),
        )
        holding_id = int(cursor.lastrowid)
    else:
        connection.execute(
            """INSERT INTO holdings
               (id, name, brokerage, asset_type, units, invested_cents, current_cents,
                current_unit_price, purchase_date, reference_date, maturity_date,
                tax_note, source_key, created_at, updated_at)
               VALUES (?, ?, ?, ?, 0, 0, 0, NULL, NULL, NULL, NULL, ?, NULL, ?, ?)""",
            (item_id, item.name, item.brokerage, item.asset_type, item.tax_note, now, now),
        )
        holding_id = item_id

    return holding_id


def create_contribution(
    connection: sqlite3.Connection,
    holding_id: int,
    item: ContributionInput,
    source_key: str | None = None,
) -> int:
    units = Decimal(str(item.units))
    unit_price = item.current_unit_price
    if unit_price is None and units > 0:
        unit_price = item.current_value / units
    now = datetime.now(get_timezone()).isoformat(timespec="seconds")
    cursor = connection.execute(
        """INSERT INTO contributions
           (holding_id, units, invested_cents, current_cents, current_unit_price,
            purchase_date, maturity_date, reference_date, tax_note, source_key, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (holding_id, float(units), to_cents(item.invested_value), to_cents(item.current_value),
         float(unit_price) if unit_price is not None else None,
         item.purchase_date.isoformat() if item.purchase_date else None,
         item.maturity_date.isoformat() if item.maturity_date else None,
         item.reference_date.isoformat() if item.reference_date else None,
         item.tax_note, source_key, now, now),
    )
    return int(cursor.lastrowid)


def update_contribution(
    connection: sqlite3.Connection,
    holding_id: int,
    contribution_id: int,
    item: ContributionInput,
) -> bool:
    units = Decimal(str(item.units))
    unit_price = item.current_unit_price
    if unit_price is None and units > 0:
        unit_price = item.current_value / units
    now = datetime.now(get_timezone()).isoformat(timespec="seconds")
    cursor = connection.execute(
        """UPDATE contributions SET units = ?, invested_cents = ?, current_cents = ?,
                  current_unit_price = ?, purchase_date = ?, maturity_date = ?, reference_date = ?, tax_note = ?, updated_at = ?
           WHERE id = ? AND holding_id = ?""",
        (float(units), to_cents(item.invested_value), to_cents(item.current_value),
         float(unit_price) if unit_price is not None else None,
         item.purchase_date.isoformat() if item.purchase_date else None,
         item.maturity_date.isoformat() if item.maturity_date else None,
         item.reference_date.isoformat() if item.reference_date else None,
         item.tax_note, now, contribution_id, holding_id),
    )
    if cursor.rowcount:
        recompute_holding(connection, holding_id)
    return cursor.rowcount > 0


def recompute_holding(connection: sqlite3.Connection, holding_id: int) -> None:
    rows = connection.execute(
        "SELECT units, invested_cents, current_cents, purchase_date, reference_date FROM contributions WHERE holding_id = ?",
        (holding_id,),
    ).fetchall()
    units = sum(float(row["units"]) for row in rows)
    invested = sum(int(row["invested_cents"]) for row in rows)
    current = sum(int(row["current_cents"]) for row in rows)
    dates = [date.fromisoformat(row["purchase_date"]) for row in rows if row["purchase_date"]]
    average_date = (
        date.fromordinal(int(sum(value.toordinal() for value in dates) / len(dates))).isoformat()
        if dates else None
    )
    reference_dates = [row["reference_date"] for row in rows if row["reference_date"]]
    reference_date = max(reference_dates) if reference_dates else None
    unit_price = current / 100 / units if units > 0 else None
    now = datetime.now(get_timezone()).isoformat(timespec="seconds")
    connection.execute(
        """UPDATE holdings SET units = ?, invested_cents = ?, current_cents = ?,
                  current_unit_price = ?, purchase_date = ?, reference_date = ?, updated_at = ?
           WHERE id = ?""",
        (units, invested, current, unit_price, average_date, reference_date, now, holding_id),
    )


def update_holding(connection: sqlite3.Connection, holding_id: int, item: HoldingInput) -> bool:
    now = datetime.now(get_timezone()).isoformat(timespec="seconds")
    cursor = connection.execute(
        """UPDATE holdings SET name = ?, brokerage = ?, asset_type = ?, maturity_date = NULL,
                  tax_note = ?, updated_at = ? WHERE id = ?""",
        (item.name, item.brokerage, item.asset_type, item.tax_note, now, holding_id),
    )
    return cursor.rowcount > 0


def apply_agora_import_items(items: list[AgoraImportItem]) -> dict:
    created = updated = 0
    changed_holding_ids: set[int] = set()
    with database() as connection:
        for item in items:
            existing = connection.execute(
                "SELECT id, holding_id, purchase_date, maturity_date FROM contributions WHERE source_key = ?",
                (item.source_key,),
            ).fetchone()
            if existing:
                holding_id = int(existing["holding_id"])
                purchase_date = item.purchase_date or (
                    date.fromisoformat(existing["purchase_date"]) if existing["purchase_date"] else None
                )
                effective_item = ContributionInput(
                    units=item.units, invested_value=item.invested_value, current_value=item.current_value,
                    current_unit_price=item.current_unit_price, purchase_date=purchase_date,
                    maturity_date=item.maturity_date or (
                        date.fromisoformat(existing["maturity_date"]) if existing["maturity_date"] else None
                    ),
                    reference_date=item.reference_date, tax_note=item.tax_note,
                )
                update_contribution(connection, holding_id, int(existing["id"]), effective_item)
                connection.execute(
                    "UPDATE holdings SET name = ?, brokerage = ?, asset_type = ? WHERE id = ?",
                    (item.name, item.brokerage, item.asset_type, holding_id),
                )
                updated += 1
            else:
                holding = connection.execute(
                    """SELECT id FROM holdings WHERE brokerage = ? COLLATE NOCASE
                       AND asset_type = ? AND name = ? COLLATE NOCASE ORDER BY id LIMIT 1""",
                    (item.brokerage, item.asset_type, item.name),
                ).fetchone()
                if holding:
                    holding_id = int(holding["id"])
                else:
                    parent = HoldingInput(
                        name=item.name, brokerage=item.brokerage, asset_type=item.asset_type,
                        units=0, invested_value=0, current_value=0,
                    )
                    holding_id = create_holding(connection, parent)
                create_contribution(connection, holding_id, item, source_key=item.source_key)
                created += 1
            changed_holding_ids.add(holding_id)

        for holding_id in changed_holding_ids:
            recompute_holding(connection, holding_id)
    return {"created": created, "updated": updated, "total": len(items)}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/login")
def login_page(request: Request):
    if has_valid_session(request):
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/")
def home(request: Request):
    if not has_valid_session(request):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/auth/login")
def login(payload: LoginPayload, response: Response) -> dict:
    with database() as connection:
        user = fetch_user_by_username(connection, payload.username)
    valid_password = False
    if user and len(payload.password.encode("utf-8")) <= 72:
        try:
            valid_password = bcrypt.checkpw(payload.password.encode("utf-8"), user["password_hash"].encode("ascii"))
        except ValueError:
            valid_password = False
    if not user or not valid_password:
        raise HTTPException(status_code=401, detail="Usuário ou senha incorretos.")
    if not user["active"]:
        raise HTTPException(status_code=403, detail="Este usuário está desativado.")
    response.set_cookie(
        COOKIE_NAME,
        make_user_session_token(user),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        path="/",
    )
    return {"ok": True}


@app.get("/api/auth/me")
def get_current_user(request: Request) -> dict:
    return public_user(current_user(request))


@app.post("/api/auth/change-password")
def change_password(payload: PasswordChangePayload, request: Request, response: Response) -> dict:
    user = current_user(request)
    if len(payload.current_password.encode("utf-8")) > 72 or not bcrypt.checkpw(
        payload.current_password.encode("utf-8"), user["password_hash"].encode("ascii")
    ):
        raise HTTPException(status_code=400, detail="A senha atual está incorreta.")
    if bcrypt.checkpw(payload.new_password.encode("utf-8"), user["password_hash"].encode("ascii")):
        raise HTTPException(status_code=400, detail="Escolha uma senha diferente da atual.")
    with database() as connection:
        connection.execute(
            """UPDATE users SET password_hash = ?, must_change_password = 0,
                      token_version = token_version + 1 WHERE id = ?""",
            (bcrypt.hashpw(payload.new_password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii"), user["id"]),
        )
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=COOKIE_SECURE, samesite="strict")
    return {"ok": True, "login_required": True}


@app.post("/api/auth/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=COOKIE_SECURE, samesite="strict")
    return {"ok": True}


@app.get("/api/admin/users", dependencies=[Depends(require_admin)])
def list_users() -> dict:
    with database() as connection:
        users = connection.execute(
            """SELECT u.*, p.can_view_portfolio, p.can_edit_positions,
                      p.can_manage_calendar, p.can_manage_backups
               FROM users u LEFT JOIN user_permissions p ON p.user_id = u.id
               ORDER BY u.role DESC, u.username COLLATE NOCASE"""
        ).fetchall()
    return {"items": [public_user(dict(user)) for user in users]}


@app.post("/api/admin/users", dependencies=[Depends(require_admin)])
def create_user(payload: AdminUserCreate) -> dict:
    now = datetime.now(get_timezone()).isoformat(timespec="seconds")
    try:
        with database() as connection:
            cursor = connection.execute(
                """INSERT INTO users
                   (username, name, password_hash, role, active, must_change_password, token_version, created_at)
                   VALUES (?, ?, ?, 'user', 1, 1, 0, ?)""",
                (payload.username, payload.name,
                 bcrypt.hashpw(payload.password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii"), now),
            )
            user_id = cursor.lastrowid
            connection.execute(
                """INSERT INTO user_permissions
                   (user_id, can_view_portfolio, can_edit_positions, can_manage_calendar, can_manage_backups)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, int(payload.permissions.can_view_portfolio), int(payload.permissions.can_edit_positions),
                 int(payload.permissions.can_manage_calendar), int(payload.permissions.can_manage_backups)),
            )
            user = fetch_user(connection, user_id)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Já existe uma conta com esse usuário.") from exc
    return public_user(dict(user))


@app.put("/api/admin/users/{user_id}", dependencies=[Depends(require_admin)])
def update_user(user_id: int, payload: AdminUserUpdate, request: Request) -> dict:
    actor = current_user(request)
    with database() as connection:
        target = fetch_user(connection, user_id)
        if not target:
            raise HTTPException(status_code=404, detail="Usuário não encontrado.")
        if target["role"] == "admin":
            raise HTTPException(status_code=403, detail="A conta administradora não pode ser alterada por este painel.")
        if actor["id"] == user_id and payload.active is False:
            raise HTTPException(status_code=400, detail="Você não pode desativar sua própria conta.")
        if payload.name is not None:
            connection.execute("UPDATE users SET name = ? WHERE id = ?", (payload.name, user_id))
        if payload.active is not None:
            connection.execute("UPDATE users SET active = ? WHERE id = ?", (int(payload.active), user_id))
        if payload.permissions is not None:
            connection.execute(
                """UPDATE user_permissions SET can_view_portfolio = ?, can_edit_positions = ?,
                          can_manage_calendar = ?, can_manage_backups = ? WHERE user_id = ?""",
                (int(payload.permissions.can_view_portfolio), int(payload.permissions.can_edit_positions),
                 int(payload.permissions.can_manage_calendar), int(payload.permissions.can_manage_backups), user_id),
            )
        password = payload.password if payload.password else None
        if password:
            connection.execute(
                """UPDATE users SET password_hash = ?, must_change_password = 1,
                          token_version = token_version + 1 WHERE id = ?""",
                (bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii"), user_id),
            )
        elif payload.active is False:
            connection.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (user_id,))
        updated = fetch_user(connection, user_id)
    return public_user(dict(updated))


@app.delete("/api/admin/users/{user_id}", dependencies=[Depends(require_admin)])
def delete_user(user_id: int, request: Request) -> dict:
    actor = current_user(request)
    if actor["id"] == user_id:
        raise HTTPException(status_code=400, detail="Você não pode excluir sua própria conta.")
    with database() as connection:
        target = connection.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="Usuário não encontrado.")
        if target["role"] == "admin":
            raise HTTPException(status_code=403, detail="A conta administradora não pode ser excluída por este painel.")
        connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return {"ok": True}


@app.get("/api/dashboard", dependencies=[Depends(require_permission("can_view_portfolio"))])
def dashboard(
    as_of: date | None = None,
    brokerage: str | None = Query(default=None, min_length=1, max_length=100),
) -> dict:
    brokerage_filter = brokerage.strip() if brokerage and brokerage.strip() else None
    with database() as connection:
        holidays = read_holiday_dates(connection)
        all_rows = list_holding_rows(connection)
        rows = [
            row for row in all_rows
            if brokerage_filter is None
            or row["brokerage"].strip().casefold() == brokerage_filter.casefold()
        ]
        holding_ids = {int(row["id"]) for row in rows}
        contribution_rows = connection.execute(
            """SELECT c.holding_id, c.purchase_date, c.invested_cents, c.current_cents, h.asset_type
               FROM contributions c JOIN holdings h ON h.id = c.holding_id"""
        ).fetchall()
        contribution_rows = [row for row in contribution_rows if int(row["holding_id"]) in holding_ids]
        dated_rows = [row["reference_date"] for row in rows if row["reference_date"]]
        reference_date = as_of or (
            max(date.fromisoformat(value) for value in dated_rows) if dated_rows else local_today()
        )
        holdings = [
            position_projection(row, reference_date, holidays)
            for row in rows
        ]

    invested_total = sum(int(row["invested_cents"]) for row in rows) / 100
    current_total = sum(int(row["current_cents"]) for row in rows) / 100
    gain_total = current_total - invested_total
    units_total = sum(float(row["units"]) for row in rows)
    total_return_pct = (current_total / invested_total - 1) if invested_total > 0 and units_total > 0 else None
    relevant_rows = [
        row for row in contribution_rows
        if int(row["invested_cents"]) > 0 or int(row["current_cents"]) > 0
    ]
    undated_positions = sum(
        1 for row in contribution_rows
        if not row["purchase_date"] and (int(row["invested_cents"]) > 0 or int(row["current_cents"]) > 0)
    )
    bases = {"calendar" if row["asset_type"] == "cripto" else "business" for row in relevant_rows}
    projection_basis = next(iter(bases)) if len(bases) == 1 else ("business" if not bases else "mixed")
    average_purchase_date = None
    dated_relevant_rows = [row for row in relevant_rows if row["purchase_date"]]
    if dated_relevant_rows:
        average_ordinal = sum(date.fromisoformat(row["purchase_date"]).toordinal() for row in dated_relevant_rows) / len(dated_relevant_rows)
        average_purchase_date = date.fromordinal(int(average_ordinal))
    basis_asset_type = "cripto" if projection_basis == "calendar" else "titulo_publico"
    days_30 = (
        projection_days(reference_date, reference_date + timedelta(days=30), holidays, basis_asset_type)
        if projection_basis != "mixed" else None
    )
    days_360 = (
        projection_days(reference_date, reference_date + timedelta(days=360), holidays, basis_asset_type)
        if projection_basis != "mixed" else None
    )
    total_elapsed = 0
    if average_purchase_date is not None and projection_basis != "mixed":
        total_elapsed = projection_days(
            average_purchase_date + timedelta(days=1),
            reference_date - timedelta(days=1),
            holidays,
            basis_asset_type,
        )
    total_daily_rate = None
    projected_next_day_total = projected_30d_total = projected_360d_total = None
    if total_return_pct is not None and total_elapsed > 0 and days_30 is not None and days_360 is not None:
        total_daily_rate = (current_total / invested_total) ** (1 / total_elapsed) - 1
        if total_elapsed >= MIN_PROJECTION_HISTORY_NEXT_DAY:
            projected_next_day_total = round(current_total * (1 + total_daily_rate), 2)
        if total_elapsed >= MIN_PROJECTION_HISTORY_30D:
            projected_30d_total = round(current_total * ((1 + total_daily_rate) ** days_30), 2)
        if total_elapsed >= MIN_PROJECTION_HISTORY_360D:
            projected_360d_total = round(current_total * ((1 + total_daily_rate) ** days_360), 2)
    projected_30d_return_pct = (
        projected_30d_total / current_total - 1
        if current_total > 0 and projected_30d_total is not None else None
    )
    projected_360d_return_pct = (
        projected_360d_total / current_total - 1
        if current_total > 0 and projected_360d_total is not None else None
    )

    return {
        "as_of": reference_date.isoformat(),
        "brokerage_filter": brokerage_filter,
        "holiday_count": len(holidays),
        "business_days_30": business_days(reference_date, reference_date + timedelta(days=30), holidays),
        "business_days_360": business_days(reference_date, reference_date + timedelta(days=360), holidays),
        "projection_days_30": days_30,
        "projection_days_360": days_360,
        "summary": {
            "holdings_count": len(holdings),
            "units_total": units_total,
            "invested_total": round(invested_total, 2),
            "current_total": round(current_total, 2),
            "gain_total": round(gain_total, 2),
            "return_pct": total_return_pct,
            "daily_rate": total_daily_rate,
            "elapsed_business_days": total_elapsed,
            "undated_positions_count": undated_positions,
            "projection_basis": projection_basis,
            "average_purchase_date": average_purchase_date.isoformat() if average_purchase_date else None,
            "projected_next_day_total": projected_next_day_total,
            "projected_30d_total": projected_30d_total,
            "projected_360d_total": projected_360d_total,
            "projected_30d_return_pct": projected_30d_return_pct,
            "projected_360d_return_pct": projected_360d_return_pct,
        },
        "holdings": holdings,
    }


@app.post("/api/holdings", dependencies=[Depends(require_permission("can_edit_positions"))])
def add_holding(item: HoldingInput) -> dict:
    with database() as connection:
        holding_id = create_holding(connection, item)
        row = fetch_holding_row(connection, holding_id)
        return row_to_holding(row)


@app.get(
    "/api/holdings/{holding_id}/contributions",
    dependencies=[Depends(require_permission("can_view_portfolio"))],
)
def get_contributions(holding_id: int, as_of: date | None = None) -> dict:
    with database() as connection:
        holding = fetch_holding_row(connection, holding_id)
        if not holding:
            raise HTTPException(status_code=404, detail="Investimento não encontrado.")
        holidays = read_holiday_dates(connection)
        contribution_rows = list_contribution_rows(connection, holding_id)
        reference_dates = [row["reference_date"] for row in contribution_rows if row["reference_date"]]
        effective_as_of = as_of or (
            max(date.fromisoformat(value) for value in reference_dates)
            if reference_dates else local_today()
        )
        return {
            "holding": position_projection(holding, effective_as_of, holidays),
            "summary": position_projection(holding, effective_as_of, holidays),
            "items": [
                contribution_projection(row, holding, effective_as_of, holidays)
                for row in contribution_rows
            ],
        }


@app.get(
    "/api/fx-rates/{currency}",
    dependencies=[Depends(require_any_permission("can_view_portfolio", "can_edit_positions"))],
)
def fx_rate(currency: Literal["BRL", "USD", "BTC"]) -> dict:
    try:
        return public_fx_quote(get_fx_quote(currency))
    except (ValueError, OSError, TimeoutError) as exc:
        label = {"USD": "USD/BRL", "BTC": "BTC/BRL"}.get(currency, currency)
        raise HTTPException(
            status_code=503,
            detail=f"Não foi possível obter a cotação atual de {label}. Tente novamente mais tarde.",
        ) from exc


@app.post(
    "/api/holdings/{holding_id}/contributions",
    dependencies=[Depends(require_permission("can_edit_positions"))],
)
def add_contribution(holding_id: int, item: ContributionInput) -> dict:
    try:
        item_brl, quote = contribution_in_brl(item)
    except (ValueError, OSError, TimeoutError) as exc:
        raise HTTPException(status_code=503, detail="Não foi possível obter a cotação necessária para converter o aporte.") from exc
    with database() as connection:
        holding = fetch_holding_row(connection, holding_id)
        if not holding:
            raise HTTPException(status_code=404, detail="Investimento não encontrado.")
        contribution_id = create_contribution(connection, holding_id, item_brl)
        recompute_holding(connection, holding_id)
        contribution = connection.execute(
            "SELECT * FROM contributions WHERE id = ?", (contribution_id,)
        ).fetchone()
        updated_holding = fetch_holding_row(connection, holding_id)
        holidays = read_holiday_dates(connection)
        as_of = date.fromisoformat(updated_holding["reference_date"]) if updated_holding["reference_date"] else local_today()
        result = contribution_projection(contribution, updated_holding, as_of, holidays)
        result["currency_conversion"] = public_fx_quote(quote)
        return result


@app.put(
    "/api/holdings/{holding_id}/contributions/{contribution_id}",
    dependencies=[Depends(require_permission("can_edit_positions"))],
)
def edit_contribution(holding_id: int, contribution_id: int, item: ContributionInput) -> dict:
    try:
        item_brl, quote = contribution_in_brl(item, convert_invested_value=False)
    except (ValueError, OSError, TimeoutError) as exc:
        raise HTTPException(status_code=503, detail="Não foi possível obter a cotação necessária para converter o aporte.") from exc
    with database() as connection:
        if not update_contribution(connection, holding_id, contribution_id, item_brl):
            raise HTTPException(status_code=404, detail="Aporte não encontrado.")
        holding = fetch_holding_row(connection, holding_id)
        contribution = connection.execute(
            "SELECT * FROM contributions WHERE id = ?", (contribution_id,)
        ).fetchone()
        holidays = read_holiday_dates(connection)
        as_of = date.fromisoformat(holding["reference_date"]) if holding["reference_date"] else local_today()
        result = contribution_projection(contribution, holding, as_of, holidays)
        result["currency_conversion"] = public_fx_quote(quote)
        return result


@app.delete(
    "/api/holdings/{holding_id}/contributions/{contribution_id}",
    dependencies=[Depends(require_permission("can_edit_positions"))],
)
def delete_contribution(holding_id: int, contribution_id: int) -> dict:
    with database() as connection:
        cursor = connection.execute(
            "DELETE FROM contributions WHERE id = ? AND holding_id = ?",
            (contribution_id, holding_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Aporte não encontrado.")
        recompute_holding(connection, holding_id)
    return {"ok": True}


@app.post("/api/holdings/import/agora/preview", dependencies=[Depends(require_permission("can_edit_positions"))])
async def preview_agora_import(request: Request) -> dict:
    try:
        parsed = parse_agora_pdf(await request.body())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with database() as connection:
        items = []
        for item in parsed["items"]:
            existing = connection.execute(
                "SELECT id, purchase_date FROM contributions WHERE source_key = ?", (item["source_key"],)
            ).fetchone()
            if existing and existing["purchase_date"]:
                item["purchase_date"] = date.fromisoformat(existing["purchase_date"])
            items.append({**item, "action": "atualizar" if existing else "criar"})
    return {"brokerage": parsed["brokerage"], "report_date": parsed["report_date"], "items": items}


@app.post("/api/holdings/import/agora/commit", dependencies=[Depends(require_permission("can_edit_positions"))])
def commit_agora_import(payload: AgoraImportCommit) -> dict:
    try:
        return apply_agora_import_items(payload.items)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Não foi possível atualizar uma posição importada.") from exc


@app.put("/api/holdings/values", dependencies=[Depends(require_permission("can_edit_positions"))])
def update_current_values(payload: QuoteUpdates) -> dict:
    ids = [item.id for item in payload.items]
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=422, detail="Há títulos repetidos na atualização.")
    now = datetime.now(get_timezone()).isoformat(timespec="seconds")
    try:
        quotes = {currency: get_fx_quote(currency) for currency in {item.currency for item in payload.items}}
    except (ValueError, OSError, TimeoutError) as exc:
        raise HTTPException(status_code=503, detail="Não foi possível obter uma das cotações necessárias.") from exc
    with database() as connection:
        for item in payload.items:
            converted_value = convert_to_brl(item.current_value, quotes[item.currency], cents=True)
            current_cents = to_cents(converted_value)
            holding = fetch_holding_row(connection, item.id)
            if not holding:
                raise HTTPException(status_code=404, detail="Um título não foi encontrado.")
            rows = list_contribution_rows(connection, item.id)
            if not rows:
                connection.execute(
                    "UPDATE holdings SET current_cents = ?, updated_at = ? WHERE id = ?",
                    (current_cents, now, item.id),
                )
                continue
            weights = [max(0, int(row["current_cents"])) for row in rows]
            if sum(weights) == 0:
                weights = [max(0, int(row["invested_cents"])) for row in rows]
            if sum(weights) == 0:
                weights = [1 for _ in rows]
            weight_total = sum(weights)
            allocated = 0
            for index, (row, weight) in enumerate(zip(rows, weights)):
                share = current_cents - allocated if index == len(rows) - 1 else (current_cents * weight + weight_total // 2) // weight_total
                allocated += share
                connection.execute(
                    """UPDATE contributions SET current_cents = ?,
                              current_unit_price = CASE WHEN units > 0 THEN ? / 100.0 / units ELSE current_unit_price END,
                              reference_date = ?, updated_at = ? WHERE id = ?""",
                    (share, share, local_today().isoformat(), now, int(row["id"])),
                )
            recompute_holding(connection, item.id)
    return {
        "ok": True,
        "updated": len(payload.items),
        "conversions": [public_fx_quote(quotes[currency]) for currency in sorted(quotes) if currency != "BRL"],
    }


@app.get(
    "/api/holdings/current-values",
    dependencies=[Depends(require_permission("can_view_portfolio"))],
)
def get_individual_current_values() -> dict:
    with database() as connection:
        holidays = read_holiday_dates(connection)
        items = []
        for holding in list_holding_rows(connection):
            rows = list_contribution_rows(connection, int(holding["id"]))
            if not rows:
                items.append({
                    "holding_id": int(holding["id"]),
                    "contribution_id": None,
                    "brokerage": holding["brokerage"],
                    "name": holding["name"],
                    "asset_type_label": ASSET_TYPE_LABELS.get(holding["asset_type"], "Outro"),
                    "purchase_date": None,
                    "units": holding["units"],
                    "current_value": money(holding["current_cents"]),
                })
                continue
            reference_dates = [date.fromisoformat(row["reference_date"]) for row in rows if row["reference_date"]]
            as_of = max(reference_dates) if reference_dates else local_today()
            for index, contribution in enumerate(rows, 1):
                projected = contribution_projection(contribution, holding, as_of, holidays)
                items.append({
                    **projected,
                    "holding_id": int(holding["id"]),
                    "contribution_id": int(contribution["id"]),
                    "brokerage": holding["brokerage"],
                    "name": holding["name"],
                    "asset_type_label": ASSET_TYPE_LABELS.get(holding["asset_type"], "Outro"),
                    "contribution_number": index,
                })
    return {"items": items}


@app.put(
    "/api/holdings/current-values",
    dependencies=[Depends(require_permission("can_edit_positions"))],
)
def update_individual_current_values(payload: IndividualValueUpdates) -> dict:
    keys = [(item.holding_id, item.contribution_id) for item in payload.items]
    if len(keys) != len(set(keys)):
        raise HTTPException(status_code=422, detail="Há aportes repetidos na atualização.")
    try:
        quotes = {currency: get_fx_quote(currency) for currency in {item.currency for item in payload.items}}
    except (ValueError, OSError, TimeoutError) as exc:
        raise HTTPException(status_code=503, detail="Não foi possível obter uma das cotações necessárias.") from exc

    now = datetime.now(get_timezone()).isoformat(timespec="seconds")
    updated_holding_ids: set[int] = set()
    with database() as connection:
        for item in payload.items:
            holding = fetch_holding_row(connection, item.holding_id)
            if not holding:
                raise HTTPException(status_code=404, detail="Um investimento não foi encontrado.")
            current_cents = to_cents(convert_to_brl(item.current_value, quotes[item.currency], cents=True))
            reference_date = local_today().isoformat()

            if item.contribution_id is None:
                if list_contribution_rows(connection, item.holding_id):
                    raise HTTPException(status_code=422, detail="Selecione um aporte individual para este investimento.")
                connection.execute(
                    """UPDATE holdings SET current_cents = ?,
                              current_unit_price = CASE WHEN units > 0 THEN ? / 100.0 / units ELSE current_unit_price END,
                              reference_date = ?, updated_at = ? WHERE id = ?""",
                    (current_cents, current_cents, reference_date, now, item.holding_id),
                )
                continue

            contribution = connection.execute(
                "SELECT units FROM contributions WHERE id = ? AND holding_id = ?",
                (item.contribution_id, item.holding_id),
            ).fetchone()
            if not contribution:
                raise HTTPException(status_code=404, detail="Um aporte não foi encontrado neste investimento.")
            connection.execute(
                """UPDATE contributions SET current_cents = ?,
                          current_unit_price = CASE WHEN units > 0 THEN ? / 100.0 / units ELSE current_unit_price END,
                          reference_date = ?, updated_at = ? WHERE id = ? AND holding_id = ?""",
                (current_cents, current_cents, reference_date, now, item.contribution_id, item.holding_id),
            )
            updated_holding_ids.add(item.holding_id)

        for holding_id in updated_holding_ids:
            recompute_holding(connection, holding_id)

    return {
        "ok": True,
        "updated": len(payload.items),
        "conversions": [public_fx_quote(quotes[currency]) for currency in sorted(quotes) if currency != "BRL"],
    }


@app.put("/api/holdings/{holding_id}", dependencies=[Depends(require_permission("can_edit_positions"))])
def edit_holding(holding_id: int, item: HoldingInput) -> dict:
    with database() as connection:
        if not update_holding(connection, holding_id, item):
            raise HTTPException(status_code=404, detail="Título não encontrado.")
        row = fetch_holding_row(connection, holding_id)
        return row_to_holding(row)


@app.delete("/api/holdings/{holding_id}", dependencies=[Depends(require_permission("can_edit_positions"))])
def delete_holding(holding_id: int) -> dict:
    with database() as connection:
        cursor = connection.execute("DELETE FROM holdings WHERE id = ?", (holding_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Título não encontrado.")
    return {"ok": True}


@app.get("/api/holidays", dependencies=[Depends(require_any_permission("can_view_portfolio", "can_manage_calendar"))])
def get_holidays(limit: int = Query(default=24, ge=1, le=100)) -> dict:
    today = local_today().isoformat()
    with database() as connection:
        count = connection.execute("SELECT COUNT(*) AS n FROM holidays").fetchone()["n"]
        rows = connection.execute(
            "SELECT holiday_date AS date, name FROM holidays WHERE holiday_date >= ? ORDER BY holiday_date LIMIT ?",
            (today, limit),
        ).fetchall()
    return {"total": count, "items": [dict(row) for row in rows]}


@app.post("/api/holidays", dependencies=[Depends(require_permission("can_manage_calendar"))])
def add_holiday(item: HolidayInput) -> dict:
    try:
        with database() as connection:
            connection.execute(
                "INSERT INTO holidays (holiday_date, name) VALUES (?, ?)",
                (item.date.isoformat(), item.name),
            )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Já existe um feriado nessa data.") from exc
    return {"date": item.date.isoformat(), "name": item.name}


@app.delete("/api/holidays/{holiday_date}", dependencies=[Depends(require_permission("can_manage_calendar"))])
def delete_holiday(holiday_date: date) -> dict:
    with database() as connection:
        cursor = connection.execute(
            "DELETE FROM holidays WHERE holiday_date = ?", (holiday_date.isoformat(),)
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Feriado não encontrado.")
    return {"ok": True}


@app.get("/api/backup", dependencies=[Depends(require_permission("can_manage_backups"))])
def export_backup() -> Response:
    with database() as connection:
        holdings = []
        for row in list_holding_rows(connection):
            item = row_to_holding(row)
            item["source_key"] = row["source_key"]
            item["contributions"] = [
                {
                    "id": contribution["id"],
                    "units": contribution["units"],
                    "invested_value": money(contribution["invested_cents"]),
                    "current_value": money(contribution["current_cents"]),
                    "current_unit_price": contribution["current_unit_price"],
                    "purchase_date": contribution["purchase_date"],
                    "maturity_date": contribution["maturity_date"],
                    "reference_date": contribution["reference_date"],
                    "tax_note": contribution["tax_note"],
                    "source_key": contribution["source_key"],
                }
                for contribution in list_contribution_rows(connection, int(row["id"]))
            ]
            holdings.append(item)
        holidays = [dict(row) for row in connection.execute(
            "SELECT holiday_date AS date, name FROM holidays ORDER BY holiday_date"
        ).fetchall()]
    payload = {
        "format_version": 1,
        "exported_at": datetime.now(get_timezone()).isoformat(timespec="seconds"),
        "holdings": holdings,
        "holidays": holidays,
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="investimentos-backup.json"'},
    )


@app.post("/api/restore", dependencies=[Depends(require_permission("can_manage_backups"))])
def restore_backup(payload: BackupPayload) -> dict:
    try:
        with database() as connection:
            connection.execute("DELETE FROM holdings")
            connection.execute("DELETE FROM holidays")
            for item in payload.holdings:
                parent = HoldingInput(
                    name=item.name, brokerage=item.brokerage, asset_type=item.asset_type,
                    maturity_date=item.maturity_date, tax_note=item.tax_note,
                )
                holding_id = create_holding(connection, parent, item.id)
                if item.contributions:
                    for contribution in item.contributions:
                        child = ContributionInput(
                            units=contribution.units,
                            invested_value=contribution.invested_value,
                            current_value=contribution.current_value,
                            current_unit_price=contribution.current_unit_price,
                            purchase_date=contribution.purchase_date,
                            maturity_date=contribution.maturity_date or item.maturity_date,
                            reference_date=contribution.reference_date,
                            tax_note=contribution.tax_note,
                        )
                        create_contribution(connection, holding_id, child, contribution.source_key)
                elif (
                    item.units > 0 or item.invested_value > 0 or item.current_value > 0
                    or item.purchase_date or item.maturity_date
                ):
                    legacy = ContributionInput(
                        units=item.units,
                        invested_value=item.invested_value,
                        current_value=item.current_value,
                        current_unit_price=item.current_unit_price,
                        purchase_date=item.purchase_date,
                        maturity_date=item.maturity_date,
                        reference_date=item.reference_date,
                        tax_note=item.tax_note,
                    )
                    create_contribution(connection, holding_id, legacy, item.source_key)
                recompute_holding(connection, holding_id)
            connection.executemany(
                "INSERT INTO holidays (holiday_date, name) VALUES (?, ?)",
                [(item.date.isoformat(), item.name) for item in payload.holidays],
            )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=400, detail="O backup contém títulos ou feriados duplicados.") from exc
    return {"ok": True, "holdings": len(payload.holdings), "holidays": len(payload.holidays)}
