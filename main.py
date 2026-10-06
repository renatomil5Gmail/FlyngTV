import os
import re
import sqlite3
import hashlib
import hmac
import logging
import calendar
import unicodedata
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mercadopago
import requests
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, field_validator


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(BASE_DIR / "clientes.db")))
try:
    PIX_EXPIRATION_MINUTES = max(10, min(1440, int(os.getenv("PIX_EXPIRATION_MINUTES", "30"))))
except ValueError:
    PIX_EXPIRATION_MINUTES = 30

app = FastAPI(title="PlayTV — Cadastro e pagamentos")
logger = logging.getLogger(__name__)
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
LIVE21_CREDENTIAL_KEY = os.getenv("LIVE21_CREDENTIAL_KEY", "").strip()
LIVE21_INLINE_MODE = os.getenv("LIVE21_INLINE_MODE", "off").strip().lower()
LIVE21_TEST_CUSTOMER_PHONE = re.sub(r"\D", "", os.getenv("LIVE21_TEST_CUSTOMER_PHONE", ""))
admin_security = HTTPBasic()
MP_ACCESS_TOKEN = os.getenv("MERCADOPAGO_TOKEN")
MP_WEBHOOK_SECRET = os.getenv("MERCADOPAGO_WEBHOOK_SECRET")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
EM_RENDER = bool(os.getenv("RENDER") or os.getenv("RENDER_SERVICE_ID"))
CUSTOMER_DB_BACKEND = "supabase" if EM_RENDER else os.getenv("CUSTOMER_DB_BACKEND", "sqlite").strip().lower()
sdk = mercadopago.SDK(MP_ACCESS_TOKEN) if MP_ACCESS_TOKEN else None
PRECO_MENSAL = 35.00
PLANOS = {1: "Plano completo — 1 mês", 3: "Plano completo — 3 meses", 6: "Plano completo — 6 meses"}


def carregar_telefone_suporte() -> str:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return re.sub(r"\D", "", os.getenv("SUPORTE_WHATSAPP", ""))
    try:
        response = requests.get(
            f"{SUPABASE_URL}/rest/v1/Contato",
            params={"select": "telefone", "order": "created_at.asc", "limit": "1"},
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            },
            timeout=5,
        )
        response.raise_for_status()
        rows = response.json()
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return re.sub(r"\D", "", str(rows[0].get("telefone", "")))
    except (requests.RequestException, ValueError, TypeError):
        logger.exception("Falha ao buscar o telefone de suporte no Supabase.")
    return re.sub(r"\D", "", os.getenv("SUPORTE_WHATSAPP", ""))


def conectar_banco():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def criar_tabelas():
    with closing(conectar_banco()) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS clientes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                telefone TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL,
                marca_tv TEXT NOT NULL,
                criado_em TEXT NOT NULL,
                atualizado_em TEXT NOT NULL,
                vigencia_ate TEXT,
                telas INTEGER NOT NULL DEFAULT 1,
                supabase_id TEXT
            );
            CREATE TABLE IF NOT EXISTS transacoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_id_mp TEXT NOT NULL UNIQUE,
                cliente_id INTEGER NOT NULL REFERENCES clientes(id),
                plano_meses INTEGER NOT NULL,
                telas INTEGER NOT NULL DEFAULT 1,
                valor REAL NOT NULL,
                status TEXT NOT NULL,
                criado_em TEXT NOT NULL,
                expira_em TEXT,
                ativado_em TEXT
            );
            CREATE TABLE IF NOT EXISTS live21_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_id_mp TEXT NOT NULL UNIQUE REFERENCES transacoes(payment_id_mp),
                cliente_id INTEGER NOT NULL REFERENCES clientes(id),
                source TEXT NOT NULL DEFAULT 'payment' CHECK (source IN ('payment', 'trial')),
                plano_meses INTEGER NOT NULL,
                telas INTEGER NOT NULL,
                expected_expira_em TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'processing', 'succeeded', 'manual_review', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0,
                error_code TEXT,
                leased_em TEXT,
                criado_em TEXT NOT NULL,
                atualizado_em TEXT NOT NULL,
                concluido_em TEXT
            );
            CREATE TABLE IF NOT EXISTS live21_contas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cliente_id INTEGER REFERENCES clientes(id) ON DELETE SET NULL,
                referencia TEXT NOT NULL,
                external_id TEXT,
                usuario_cifrado BLOB,
                senha_cifrada BLOB,
                dados_cifrados BLOB,
                expira_em TEXT,
                criado_em TEXT NOT NULL,
                atualizado_em TEXT NOT NULL,
                conta_teste INTEGER NOT NULL DEFAULT 0 CHECK (conta_teste IN (0, 1)),
                CHECK ((usuario_cifrado IS NOT NULL AND senha_cifrada IS NOT NULL) OR dados_cifrados IS NOT NULL)
            );
            """
        )
        colunas_jobs = {row["name"] for row in connection.execute("PRAGMA table_info(live21_jobs)")}
        if "source" not in colunas_jobs:
            connection.execute("ALTER TABLE live21_jobs ADD COLUMN source TEXT NOT NULL DEFAULT 'payment'")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS live21_trial_active_customer_unique ON live21_jobs(cliente_id) WHERE source = 'trial' AND status IN ('queued', 'processing')"
        )
        colunas_live21 = {
            row["name"] for row in connection.execute("PRAGMA table_info(live21_contas)")
        }
        if "dados_cifrados" not in colunas_live21:
            connection.executescript(
                """
                CREATE TABLE live21_contas_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cliente_id INTEGER REFERENCES clientes(id) ON DELETE SET NULL,
                    referencia TEXT NOT NULL,
                    usuario_cifrado BLOB,
                    senha_cifrada BLOB,
                    dados_cifrados BLOB,
                    expira_em TEXT,
                    criado_em TEXT NOT NULL,
                    atualizado_em TEXT NOT NULL,
                    conta_teste INTEGER NOT NULL DEFAULT 0 CHECK (conta_teste IN (0, 1)),
                    CHECK ((usuario_cifrado IS NOT NULL AND senha_cifrada IS NOT NULL) OR dados_cifrados IS NOT NULL)
                );
                INSERT INTO live21_contas_v2
                    (id, cliente_id, referencia, usuario_cifrado, senha_cifrada, expira_em, criado_em, atualizado_em, conta_teste)
                SELECT id, cliente_id, referencia, usuario_cifrado, senha_cifrada, expira_em, criado_em, atualizado_em, conta_teste
                FROM live21_contas;
                DROP TABLE live21_contas;
                ALTER TABLE live21_contas_v2 RENAME TO live21_contas;
                """
            )
        colunas_live21 = {
            row["name"] for row in connection.execute("PRAGMA table_info(live21_contas)")
        }
        if "external_id" not in colunas_live21:
            connection.execute("ALTER TABLE live21_contas ADD COLUMN external_id TEXT")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS live21_external_id_unique ON live21_contas(external_id) WHERE external_id IS NOT NULL"
        )
        colunas_clientes = {
            row["name"] for row in connection.execute("PRAGMA table_info(clientes)")
        }
        if "vigencia_ate" not in colunas_clientes:
            connection.execute("ALTER TABLE clientes ADD COLUMN vigencia_ate TEXT")
        if "telas" not in colunas_clientes:
            connection.execute("ALTER TABLE clientes ADD COLUMN telas INTEGER NOT NULL DEFAULT 1")
        if "supabase_id" not in colunas_clientes:
            connection.execute("ALTER TABLE clientes ADD COLUMN supabase_id TEXT")
        colunas_transacoes = {
            row["name"] for row in connection.execute("PRAGMA table_info(transacoes)")
        }
        if "expira_em" not in colunas_transacoes:
            connection.execute("ALTER TABLE transacoes ADD COLUMN expira_em TEXT")
        if "ativado_em" not in colunas_transacoes:
            connection.execute("ALTER TABLE transacoes ADD COLUMN ativado_em TEXT")
        if "telas" not in colunas_transacoes:
            connection.execute("ALTER TABLE transacoes ADD COLUMN telas INTEGER NOT NULL DEFAULT 1")
        connection.commit()


criar_tabelas()


def supabase_configurado() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)


def garantir_backend_clientes():
    if CUSTOMER_DB_BACKEND == "supabase" and not supabase_configurado():
        raise HTTPException(
            status_code=503,
            detail="O serviço está configurado para usar o Supabase, mas SUPABASE_URL ou SUPABASE_SERVICE_ROLE_KEY não está configurada no Render.",
        )


def requisicao_supabase(metodo: str, tabela: str, *, params: dict | None = None, payload: dict | None = None, prefer: str | None = None):
    if not supabase_configurado():
        raise HTTPException(status_code=503, detail="A conexão com o Supabase não está configurada no servidor.")
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    try:
        response = requests.request(
            metodo,
            f"{SUPABASE_URL}/rest/v1/{tabela}",
            params=params,
            json=payload,
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        logger.warning("Supabase %s %s failed (HTTP %s).", metodo, tabela, status or "network error")
        if status == 400:
            raise HTTPException(status_code=400, detail="O Supabase recusou o formato deste filtro.") from exc
        raise HTTPException(
            status_code=503,
            detail="Não foi possível consultar ou salvar o cadastro no Supabase. Tente novamente ou fale com o suporte.",
        ) from exc
    if not response.content:
        data = None
    else:
        try:
            data = response.json()
        except ValueError as exc:
            raise HTTPException(status_code=502, detail="O Supabase retornou uma resposta inválida.") from exc
    if prefer and "count=exact" in prefer:
        total_value = response.headers.get("content-range", "").rsplit("/", 1)[-1]
        return {"data": data, "total": int(total_value) if total_value.isdigit() else 0}
    return data


def normalizar_telefone(telefone: str) -> str:
    digits = re.sub(r"\D", "", telefone)
    if len(digits) in (12, 13) and digits.startswith("55"):
        return digits[2:]
    return digits


def variantes_telefone(telefone: str) -> list[str]:
    digits = re.sub(r"\D", "", telefone)
    local = normalizar_telefone(digits)
    candidates = [digits, local]
    if len(local) in (10, 11):
        ddd, numero = local[:2], local[2:]
        candidates.extend((
            f"55{local}",
            f"+55{local}",
            f"({ddd}) {numero}",
            f"{ddd} {numero}",
            f"+55 ({ddd}) {numero}",
            f"55 ({ddd}) {numero}",
            f"+55 {ddd} {numero}",
            f"55 {ddd} {numero}",
        ))
        if len(numero) == 9:
            candidates.extend((
                f"({ddd}) {numero[:5]}-{numero[5:]}",
                f"{ddd} {numero[:5]}-{numero[5:]}",
                f"+55 ({ddd}) {numero[:5]}-{numero[5:]}",
                f"+55 {ddd} {numero[:5]}-{numero[5:]}",
                f"55 {ddd} {numero[:5]}-{numero[5:]}",
            ))
        elif len(numero) == 8:
            candidates.extend((
                f"({ddd}) {numero[:4]}-{numero[4:]}",
                f"{ddd} {numero[:4]}-{numero[4:]}",
                f"+55 ({ddd}) {numero[:4]}-{numero[4:]}",
                f"+55 {ddd} {numero[:4]}-{numero[4:]}",
                f"55 {ddd} {numero[:4]}-{numero[4:]}",
            ))
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def buscar_cliente_supabase(telefone: str) -> dict | None:
    telefone_canonico = normalizar_telefone(telefone)
    for variante in variantes_telefone(telefone):
        try:
            rows = requisicao_supabase(
                "GET",
                "clientes",
                params={"select": "*", "telefone": f"eq.{variante}", "limit": "1"},
            )
        except HTTPException as exc:
            if exc.status_code == 400:
                continue
            raise
        if not isinstance(rows, list):
            continue
        for cliente in rows:
            if (
                isinstance(cliente, dict)
                and normalizar_telefone(str(cliente.get("telefone", ""))) == telefone_canonico
            ):
                return cliente

    # Handles separators or spaces not covered by the known Brazilian formats.
    sufixo = telefone_canonico[-7:]
    try:
        rows = requisicao_supabase(
            "GET",
            "clientes",
            params={"select": "*", "telefone": f"ilike.*{sufixo}*", "limit": "100"},
        )
    except HTTPException as exc:
        if exc.status_code != 400:
            raise
        rows = []
    if isinstance(rows, list):
        for cliente in rows:
            if (
                isinstance(cliente, dict)
                and normalizar_telefone(str(cliente.get("telefone", ""))) == telefone_canonico
            ):
                return cliente
    return None


def cachear_cliente_supabase(cliente: dict) -> dict:
    telefone = re.sub(r"\D", "", str(cliente.get("telefone", "")))
    if not telefone:
        raise HTTPException(status_code=502, detail="O registro do Supabase não contém telefone válido.")
    with closing(conectar_banco()) as connection:
        atual = connection.execute(
            "SELECT email, vigencia_ate FROM clientes WHERE telefone = ?", (telefone,)
        ).fetchone()
        email = str(cliente.get("email") or (atual["email"] if atual else ""))
        vigencia_ate = cliente.get("vigencia_ate") or (atual["vigencia_ate"] if atual else None)
        agora = agora_utc().isoformat()
        connection.execute(
            """
            INSERT INTO clientes (nome, telefone, email, marca_tv, criado_em, atualizado_em, vigencia_ate, telas, supabase_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telefone) DO UPDATE SET
                nome = excluded.nome,
                email = CASE WHEN excluded.email <> '' THEN excluded.email ELSE clientes.email END,
                marca_tv = excluded.marca_tv,
                atualizado_em = excluded.atualizado_em,
                vigencia_ate = COALESCE(excluded.vigencia_ate, clientes.vigencia_ate),
                telas = excluded.telas,
                supabase_id = COALESCE(excluded.supabase_id, clientes.supabase_id)
            """,
            (
                str(cliente.get("nome") or "Cliente"),
                telefone,
                email,
                str(cliente.get("marca_tv") or "Não informado"),
                str(cliente.get("created_at") or agora),
                agora,
                vigencia_ate,
                max(1, min(4, int(cliente.get("telas") or 1))),
                str(cliente["id"]) if cliente.get("id") is not None else None,
            ),
        )
        cached = connection.execute(
            "SELECT id, nome, telefone, email, marca_tv, vigencia_ate, telas, supabase_id FROM clientes WHERE telefone = ?",
            (telefone,),
        ).fetchone()
        connection.commit()
    return dict(cached)


def salvar_cliente_supabase(req: "CadastroClienteRequest") -> dict:
    existente = buscar_cliente_supabase(req.telefone)
    payload = {
        "nome": req.nome,
        "telefone": req.telefone,
        "email": req.email,
        "marca_tv": req.marca_tv,
    }
    if existente is None:
        payload["telas"] = req.telas
        rows = requisicao_supabase(
            "POST",
            "clientes",
            params={"on_conflict": "telefone"},
            payload=payload,
            prefer="resolution=merge-duplicates,return=representation",
        )
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            existente = buscar_cliente_supabase(req.telefone)
        else:
            existente = rows[0]
    else:
        # Tela ativa de um cliente existente só muda quando o novo pagamento é aprovado.
        requisicao_supabase(
            "PATCH",
            "clientes",
            params={"id": f"eq.{existente['id']}"},
            payload=payload,
            prefer="return=representation",
        )
        existente = buscar_cliente_supabase(req.telefone) or {**existente, **payload}
    if existente is None:
        raise HTTPException(status_code=502, detail="O Supabase não confirmou o cadastro do cliente.")
    return cachear_cliente_supabase(existente)


class ConsultaTelefoneRequest(BaseModel):
    telefone: str = Field(min_length=10, max_length=20)


class CadastroClienteRequest(BaseModel):
    nome: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=254)
    telefone: str = Field(min_length=10, max_length=20)
    marca_tv: str = Field(min_length=1, max_length=60)
    telas: int = Field(default=1, ge=1, le=4)

    @field_validator("nome", "email", "telefone", "marca_tv")
    @classmethod
    def limpar_campos(cls, value: str) -> str:
        return value.strip()

    @field_validator("email")
    @classmethod
    def validar_email(cls, value: str) -> str:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Informe um e-mail válido.")
        return value

    @field_validator("telefone")
    @classmethod
    def validar_telefone(cls, value: str) -> str:
        digits = re.sub(r"\D", "", value)
        if not 10 <= len(digits) <= 13:
            raise ValueError("Informe o DDD e o número do WhatsApp.")
        if len(digits) in (12, 13) and digits.startswith("55"):
            digits = digits[2:]
        return digits


class GerarPixRequest(BaseModel):
    cliente_id: int = Field(gt=0)
    plano_meses: int
    telas: int = Field(default=1, ge=1, le=4)


class CredenciaisLive21TesteRequest(BaseModel):
    referencia: str = Field(min_length=1, max_length=120)
    usuario: str = Field(min_length=1, max_length=120)
    senha: str = Field(min_length=1, max_length=240)
    expira_em: str | None = Field(default=None, max_length=40)

    @field_validator("referencia", "usuario")
    @classmethod
    def limpar_credencial_publica(cls, value: str) -> str:
        return value.strip()


class DadosLive21TesteRequest(BaseModel):
    referencia: str = Field(min_length=1, max_length=120)
    dados: str = Field(min_length=1, max_length=5000)
    expira_em: str | None = Field(default=None, max_length=40)
    external_id: str | None = Field(default=None, max_length=80)

    @field_validator("referencia")
    @classmethod
    def limpar_referencia(cls, value: str) -> str:
        return value.strip()


def cifra_live21() -> Fernet:
    key = LIVE21_CREDENTIAL_KEY
    if not key:
        if EM_RENDER:
            raise HTTPException(status_code=503, detail="Configure LIVE21_CREDENTIAL_KEY nos secrets do servidor.")
        key_path = BASE_DIR / ".live21-credentials.key"
        try:
            key_bytes = key_path.read_bytes()
        except FileNotFoundError:
            generated_key = Fernet.generate_key()
            try:
                descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                key_bytes = key_path.read_bytes()
            else:
                with os.fdopen(descriptor, "wb") as key_file:
                    key_file.write(generated_key)
                key_bytes = generated_key
        key = key_bytes.decode("ascii").strip()
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=503, detail="LIVE21_CREDENTIAL_KEY não é uma chave Fernet válida.") from exc


def salvar_credenciais_live21_teste(req: CredenciaisLive21TesteRequest) -> int:
    cipher = cifra_live21()
    agora = agora_utc().isoformat()
    with closing(conectar_banco()) as connection:
        cursor = connection.execute(
            """
            INSERT INTO live21_contas
                (referencia, usuario_cifrado, senha_cifrada, expira_em, criado_em, atualizado_em, conta_teste)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (
                req.referencia,
                cipher.encrypt(req.usuario.encode("utf-8")),
                cipher.encrypt(req.senha.encode("utf-8")),
                req.expira_em,
                agora,
                agora,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


def salvar_dados_live21_teste(req: DadosLive21TesteRequest) -> int:
    cipher = cifra_live21()
    agora = agora_utc().isoformat()
    usuario, senha = extrair_credenciais_live21(req.dados)
    with closing(conectar_banco()) as connection:
        cursor = connection.execute(
            """
            INSERT INTO live21_contas
                (referencia, external_id, usuario_cifrado, senha_cifrada, dados_cifrados, expira_em, criado_em, atualizado_em, conta_teste)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                req.referencia,
                req.external_id,
                cipher.encrypt(usuario.encode("utf-8")) if usuario else None,
                cipher.encrypt(senha.encode("utf-8")) if senha else None,
                cipher.encrypt(req.dados.encode("utf-8")),
                req.expira_em,
                agora,
                agora,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


def extrair_credenciais_live21(dados: str) -> tuple[str | None, str | None]:
    usuario = None
    senha = None
    lines = [line.strip() for line in dados.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        label, separator, value = line.partition(":")
        normalized_label = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode("ascii").strip().lower()
        value = value.strip() if separator else (lines[index + 1] if index + 1 < len(lines) else "")
        if normalized_label in ("usuario", "user", "login"):
            usuario = value or None
        elif normalized_label in ("senha", "password", "pass"):
            senha = value or None
    return usuario, senha


def recuperar_credenciais_live21_teste(conta_id: int) -> dict | None:
    cipher = cifra_live21()
    with closing(conectar_banco()) as connection:
        conta = connection.execute(
            "SELECT referencia, external_id, usuario_cifrado, senha_cifrada, dados_cifrados, expira_em FROM live21_contas WHERE id = ? AND conta_teste = 1",
            (conta_id,),
        ).fetchone()
    if conta is None:
        return None
    try:
        usuario = cipher.decrypt(bytes(conta["usuario_cifrado"])).decode("utf-8") if conta["usuario_cifrado"] else None
        senha = cipher.decrypt(bytes(conta["senha_cifrada"])).decode("utf-8") if conta["senha_cifrada"] else None
        if (not usuario or not senha) and conta["dados_cifrados"] is not None:
            dados = cipher.decrypt(bytes(conta["dados_cifrados"])).decode("utf-8")
            usuario, senha = extrair_credenciais_live21(dados)
            if usuario and senha:
                connection = conectar_banco()
                with closing(connection) as update_connection:
                    update_connection.execute(
                        "UPDATE live21_contas SET usuario_cifrado = ?, senha_cifrada = ? WHERE id = ?",
                        (cipher.encrypt(usuario.encode("utf-8")), cipher.encrypt(senha.encode("utf-8")), conta_id),
                    )
                    update_connection.commit()
        if not usuario or not senha:
            return None
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=500, detail="Não foi possível descriptografar a conta de teste Live21.") from exc
    return {
        "referencia": conta["referencia"],
        "external_id": conta["external_id"],
        "usuario": usuario,
        "senha": senha,
        "expira_em": conta["expira_em"],
    }


def atualizar_conta_live21_teste(conta_id: int, *, external_id: str | None, expira_em: str) -> None:
    with closing(conectar_banco()) as connection:
        connection.execute(
            "UPDATE live21_contas SET external_id = COALESCE(?, external_id), expira_em = ?, atualizado_em = ? WHERE id = ? AND conta_teste = 1",
            (external_id, expira_em, agora_utc().isoformat(), conta_id),
        )
        connection.commit()


def salvar_conta_live21_cliente(
    cliente_id: int,
    *,
    external_id: str,
    usuario: str,
    senha: str,
    expira_em: str,
) -> None:
    cipher = cifra_live21()
    now = agora_utc().isoformat()
    with closing(conectar_banco()) as connection:
        existing = connection.execute(
            "SELECT id FROM live21_contas WHERE cliente_id = ? AND conta_teste = 0 ORDER BY id DESC LIMIT 1",
            (cliente_id,),
        ).fetchone()
        values = (
            external_id,
            cipher.encrypt(usuario.encode("utf-8")),
            cipher.encrypt(senha.encode("utf-8")),
            expira_em,
            now,
        )
        if existing:
            connection.execute(
                """
                UPDATE live21_contas SET external_id = ?, usuario_cifrado = ?, senha_cifrada = ?,
                    dados_cifrados = NULL, expira_em = ?, atualizado_em = ? WHERE id = ?
                """,
                (*values, existing["id"]),
            )
        else:
            connection.execute(
                """
                INSERT INTO live21_contas
                    (cliente_id, referencia, external_id, usuario_cifrado, senha_cifrada, expira_em, criado_em, atualizado_em, conta_teste)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    cliente_id,
                    f"Cliente {cliente_id}",
                    external_id,
                    cipher.encrypt(usuario.encode("utf-8")),
                    cipher.encrypt(senha.encode("utf-8")),
                    expira_em,
                    now,
                    now,
                ),
            )
        connection.commit()


def obter_conta_live21_cliente(cliente_id: int) -> dict | None:
    cipher = cifra_live21()
    with closing(conectar_banco()) as connection:
        row = connection.execute(
            """
            SELECT id, external_id, usuario_cifrado, senha_cifrada, expira_em
            FROM live21_contas WHERE cliente_id = ? AND conta_teste = 0
            ORDER BY id DESC LIMIT 1
            """,
            (cliente_id,),
        ).fetchone()
    if row is None or not row["usuario_cifrado"] or not row["senha_cifrada"]:
        return None
    try:
        return {
            "id": int(row["id"]),
            "external_id": row["external_id"],
            "usuario": cipher.decrypt(bytes(row["usuario_cifrado"])).decode("utf-8"),
            "senha": cipher.decrypt(bytes(row["senha_cifrada"])).decode("utf-8"),
            "expira_em": row["expira_em"],
        }
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=500, detail="Não foi possível descriptografar a conta Live21 vinculada.") from exc


def claim_live21_job(lease_minutes: int = 5, job_id: int | None = None) -> dict | None:
    now = agora_utc()
    stale_before = (now - timedelta(minutes=lease_minutes)).isoformat()
    with closing(conectar_banco()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        job_filter = "AND j.id = ?" if job_id is not None else ""
        params: tuple[object, ...] = (stale_before, job_id) if job_id is not None else (stale_before,)
        job = connection.execute(
            f"""
            SELECT j.id FROM live21_jobs j
            WHERE j.attempts < 3 AND (
                j.status = 'queued' OR (j.status = 'processing' AND j.leased_em < ?)
            ) {job_filter}
            ORDER BY j.criado_em ASC LIMIT 1
            """,
            params,
        ).fetchone()
        if job is None:
            connection.commit()
            return None
        connection.execute(
            "UPDATE live21_jobs SET status = 'processing', attempts = attempts + 1, leased_em = ?, atualizado_em = ?, error_code = NULL WHERE id = ?",
            (now.isoformat(), now.isoformat(), job["id"]),
        )
        claimed = connection.execute(
            """
            SELECT j.id, j.payment_id_mp, j.cliente_id, j.source, j.plano_meses, j.telas, j.expected_expira_em, j.attempts,
                c.nome, c.email, c.telefone, c.marca_tv, t.valor
            FROM live21_jobs j JOIN clientes c ON c.id = j.cliente_id
            LEFT JOIN transacoes t ON t.payment_id_mp = j.payment_id_mp
            WHERE j.id = ?
            """,
            (job["id"],),
        ).fetchone()
        connection.commit()
    return dict(claimed) if claimed else None


def finalizar_live21_job(job_id: int, *, error_code: str | None = None) -> None:
    now = agora_utc().isoformat()
    with closing(conectar_banco()) as connection:
        job = connection.execute(
            "SELECT attempts FROM live21_jobs WHERE id = ? AND status = 'processing'",
            (job_id,),
        ).fetchone()
        if job is None:
            return
        if error_code is None:
            status = "succeeded"
            completed_at = now
        elif int(job["attempts"]) >= 3:
            status = "manual_review"
            completed_at = None
        else:
            status = "queued"
            completed_at = None
        connection.execute(
            "UPDATE live21_jobs SET status = ?, error_code = ?, leased_em = NULL, atualizado_em = ?, concluido_em = ? WHERE id = ?",
            (status, error_code, now, completed_at, job_id),
        )
        connection.commit()


def ativar_vigencia_teste_live21(cliente_id: int, vigencia_ate: str) -> None:
    with closing(conectar_banco()) as connection:
        cliente = connection.execute(
            "SELECT supabase_id FROM clientes WHERE id = ?",
            (cliente_id,),
        ).fetchone()
    if cliente is None:
        raise HTTPException(status_code=404, detail="Cadastro do teste não encontrado.")
    if supabase_configurado():
        if not cliente["supabase_id"]:
            raise HTTPException(status_code=503, detail="Cadastro do teste sem vínculo Supabase.")
        requisicao_supabase(
            "PATCH",
            "clientes",
            params={"id": f"eq.{cliente['supabase_id']}"},
            payload={"vigencia_ate": vigencia_ate, "telas": 1},
            prefer="return=minimal",
        )
    with closing(conectar_banco()) as connection:
        connection.execute(
            "UPDATE clientes SET vigencia_ate = ?, telas = 1, atualizado_em = ? WHERE id = ?",
            (vigencia_ate, agora_utc().isoformat(), cliente_id),
        )
        connection.commit()


def agora_utc() -> datetime:
    return datetime.now(timezone.utc)


def adicionar_meses(data: datetime, meses: int) -> datetime:
    mes_indice = data.month - 1 + meses
    ano = data.year + mes_indice // 12
    mes = mes_indice % 12 + 1
    dia = min(data.day, calendar.monthrange(ano, mes)[1])
    return data.replace(year=ano, month=mes, day=dia)


def interpretar_data(data: str | None) -> datetime | None:
    if not data:
        return None
    try:
        parsed = datetime.fromisoformat(data.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def registrar_status_pagamento(payment_id: str, payment: dict) -> dict | None:
    if str(payment.get("id", "")) != payment_id:
        return None
    with closing(conectar_banco()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        transacao = connection.execute(
            "SELECT cliente_id, plano_meses, telas, valor, ativado_em, expira_em FROM transacoes WHERE payment_id_mp = ?",
            (payment_id,),
        ).fetchone()
        if transacao is None:
            return None

        try:
            valor_pago = float(payment.get("transaction_amount"))
        except (TypeError, ValueError):
            return None
        if (
            str(payment.get("external_reference", "")) != str(transacao["cliente_id"])
            or round(valor_pago, 2) != round(float(transacao["valor"]), 2)
        ):
            return None

        status = str(payment.get("status", "pending"))
        agora = agora_utc()
        expiracao_local = interpretar_data(transacao["expira_em"])
        if status == "pending" and expiracao_local and agora >= expiracao_local:
            status = "expired"
        vigencia_ate = None
        connection.execute(
            "UPDATE transacoes SET status = ? WHERE payment_id_mp = ?",
            (status, payment_id),
        )
        if status == "approved" and transacao["ativado_em"] is None:
            cliente = connection.execute(
                "SELECT vigencia_ate, supabase_id FROM clientes WHERE id = ?",
                (transacao["cliente_id"],),
            ).fetchone()
            if cliente is None:
                return None
            vigencia_atual = interpretar_data(cliente["vigencia_ate"])
            inicio_vigencia = max(agora, vigencia_atual) if vigencia_atual else agora
            vigencia_ate = inicio_vigencia + timedelta(days=30 * int(transacao["plano_meses"]))
            if supabase_configurado():
                if not cliente["supabase_id"]:
                    raise HTTPException(status_code=503, detail="O cadastro não está vinculado ao Supabase; fale com o suporte antes de ativar a renovação.")
                requisicao_supabase(
                    "PATCH",
                    "clientes",
                    params={"id": f"eq.{cliente['supabase_id']}"},
                    payload={
                        "vigencia_ate": vigencia_ate.isoformat(),
                        "telas": int(transacao["telas"]),
                    },
                    prefer="return=minimal",
                )
            connection.execute(
                "UPDATE clientes SET vigencia_ate = ?, telas = ?, atualizado_em = ? WHERE id = ?",
                (vigencia_ate.isoformat(), int(transacao["telas"]), agora.isoformat(), transacao["cliente_id"]),
            )
            connection.execute(
                "UPDATE transacoes SET ativado_em = ? WHERE payment_id_mp = ? AND ativado_em IS NULL",
                (agora.isoformat(), payment_id),
            )
            job_status = "queued" if int(transacao["plano_meses"]) == 1 and int(transacao["telas"]) == 1 else "manual_review"
            connection.execute(
                """
                INSERT OR IGNORE INTO live21_jobs
                    (payment_id_mp, cliente_id, plano_meses, telas, expected_expira_em, status, error_code, criado_em, atualizado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payment_id,
                    int(transacao["cliente_id"]),
                    int(transacao["plano_meses"]),
                    int(transacao["telas"]),
                    vigencia_ate.isoformat(),
                    job_status,
                    None if job_status == "queued" else "manual_review_plan_or_screens",
                    agora.isoformat(),
                    agora.isoformat(),
                ),
            )
        else:
            cliente = connection.execute(
                "SELECT vigencia_ate FROM clientes WHERE id = ?",
                (transacao["cliente_id"],),
            ).fetchone()
            if cliente is not None:
                vigencia_ate = interpretar_data(cliente["vigencia_ate"])
        connection.commit()

    return {
        "status": status,
        "vigencia_ate": vigencia_ate.isoformat() if vigencia_ate else None,
    }


def processar_job_live21_inline(job_id: int) -> None:
    try:
        from live21_worker import process_job_once

        process_job_once(job_id)
    except Exception as exc:
        logger.warning("Live21 inline job %s failed (%s).", job_id, type(exc).__name__)
        finalizar_live21_job(job_id, error_code=type(exc).__name__[:80])


def agendar_job_live21_inline(background_tasks: BackgroundTasks, payment_id: str) -> None:
    if LIVE21_INLINE_MODE != "test":
        return
    with closing(conectar_banco()) as connection:
        job = connection.execute(
            """
            SELECT j.id, j.status, j.leased_em, c.telefone
            FROM live21_jobs j JOIN clientes c ON c.id = j.cliente_id
            WHERE j.payment_id_mp = ?
            """,
            (payment_id,),
        ).fetchone()
    if job is None or job["status"] not in ("queued", "processing"):
        return
    if not LIVE21_TEST_CUSTOMER_PHONE:
        with closing(conectar_banco()) as connection:
            connection.execute(
                "UPDATE live21_jobs SET status = 'manual_review', error_code = 'test_phone_not_configured', atualizado_em = ? WHERE id = ? AND status = 'queued'",
                (agora_utc().isoformat(), job["id"]),
            )
            connection.commit()
        return
    if normalizar_telefone(str(job["telefone"])) != normalizar_telefone(LIVE21_TEST_CUSTOMER_PHONE):
        return
    if job["status"] == "processing":
        lease_time = interpretar_data(job["leased_em"])
        if lease_time and agora_utc() - lease_time < timedelta(minutes=5):
            return
    background_tasks.add_task(processar_job_live21_inline, int(job["id"]))


@app.get("/", response_class=HTMLResponse)
def pagina_inicial():
    return HTMLResponse((BASE_DIR / "index.html").read_text(encoding="utf-8"))


def autenticar_admin(credentials: HTTPBasicCredentials = Depends(admin_security)) -> None:
    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="Configure ADMIN_USERNAME e ADMIN_PASSWORD no servidor.")
    usuario_valido = hmac.compare_digest(credentials.username.encode("utf-8"), ADMIN_USERNAME.encode("utf-8"))
    senha_valida = hmac.compare_digest(credentials.password.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8"))
    if not (usuario_valido & senha_valida):
        raise HTTPException(
            status_code=401,
            detail="Credenciais administrativas inválidas.",
            headers={"WWW-Authenticate": "Basic"},
        )


def filtros_supabase_admin(situacao: str) -> dict[str, str]:
    agora = agora_utc()
    filtros: dict[str, str] = {}
    if situacao == "ativos":
        filtros["vigencia_ate"] = f"gte.{agora.isoformat()}"
    elif situacao == "vencidos":
        filtros["vigencia_ate"] = f"lt.{agora.isoformat()}"
    elif situacao == "vencendo":
        filtros["and"] = f"(vigencia_ate.gte.{agora.isoformat()},vigencia_ate.lte.{(agora + timedelta(days=7)).isoformat()})"
    elif situacao == "sem-vigencia":
        filtros["vigencia_ate"] = "is.null"
    return filtros


def contar_clientes_admin(filtros: dict[str, str] | None = None) -> int:
    resultado = requisicao_supabase(
        "GET",
        "clientes",
        params={"select": "id", "limit": "1", **(filtros or {})},
        prefer="count=exact",
    )
    return int(resultado.get("total", 0)) if isinstance(resultado, dict) else 0


@app.get("/admin", response_class=HTMLResponse)
def pagina_admin(_: None = Depends(autenticar_admin)):
    return HTMLResponse(
        (BASE_DIR / "admin.html").read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/admin/live21/capture", response_class=HTMLResponse)
def pagina_captura_live21(_: None = Depends(autenticar_admin)):
    html = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Guardar credenciais Live21</title>
<style>
body{margin:0;background:#f3f6f8;color:#172438;font:14px 'DM Sans',sans-serif}main{width:min(680px,calc(100% - 32px));margin:42px auto}h1{font:700 24px 'Manrope',sans-serif}form{display:grid;gap:14px}label{display:grid;gap:7px;font-weight:700}input,textarea,button{font:inherit}input{padding:11px;border:1px solid #dce3e9;border-radius:6px;background:white}textarea{position:fixed;top:-2000px;left:0;width:1px;height:1px;padding:0;border:0;opacity:0}button{min-height:42px;padding:0 16px;border:0;border-radius:6px;color:white;background:#146c9b;font-weight:700}#result{min-height:22px;color:#18744f}
</style></head><body><main><h1>Guardar dados do teste Live21</h1>
<form id="capture"><label>Referência do teste<input name="referencia" value="RPA TESTE" required maxlength="120"></label>
<label>Expiração<input name="expira_em" value="2026-11-06" placeholder="YYYY-MM-DD"></label>
<textarea id="clipboard-sink" aria-hidden="true" tabindex="-1" autocomplete="off"></textarea>
<button type="button" id="save-copy">Guardar credenciais copiadas</button><p id="result" role="status"></p></form>
<script>
document.getElementById('save-copy').addEventListener('click',async()=>{const form=document.getElementById('capture');const button=document.getElementById('save-copy');const sink=document.getElementById('clipboard-sink');const result=document.getElementById('result');button.disabled=true;result.textContent='';try{const dados=sink.value;if(!dados.trim())throw new Error('Cole os dados copiados do painel antes de salvar.');const values=Object.fromEntries(new FormData(form));values.dados=dados;const response=await fetch('/api/admin/live21/test-data',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values),cache:'no-store'});const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.detail||'Falha ao guardar.');sink.value='';result.textContent='Dados guardados cifrados. Registro '+data.id+'.';}catch(error){result.textContent=error.message;}finally{button.disabled=false;}});
</script>
</script></main></body></html>"""
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.post("/api/admin/live21/test-data", status_code=201)
def receber_dados_live21_teste(
    req: DadosLive21TesteRequest,
    response: Response,
    _: None = Depends(autenticar_admin),
):
    conta_id = salvar_dados_live21_teste(req)
    response.headers["Cache-Control"] = "no-store"
    return {"stored": True, "id": conta_id}


@app.get("/api/admin/resumo")
def resumo_admin(response: Response, _: None = Depends(autenticar_admin)):
    garantir_backend_clientes()
    agora = agora_utc()
    limite_vencimento = agora + timedelta(days=7)
    if supabase_configurado():
        total_clientes = contar_clientes_admin()
        clientes_ativos = contar_clientes_admin(filtros_supabase_admin("ativos"))
        clientes_vencidos = contar_clientes_admin(filtros_supabase_admin("vencidos"))
        clientes_vencendo = contar_clientes_admin(filtros_supabase_admin("vencendo"))
    else:
        with closing(conectar_banco()) as connection:
            contagens = connection.execute(
                """
                SELECT COUNT(*) AS total,
                    SUM(CASE WHEN datetime(vigencia_ate) >= datetime(?) THEN 1 ELSE 0 END) AS ativos,
                    SUM(CASE WHEN datetime(vigencia_ate) < datetime(?) THEN 1 ELSE 0 END) AS vencidos,
                    SUM(CASE WHEN datetime(vigencia_ate) >= datetime(?) AND datetime(vigencia_ate) <= datetime(?) THEN 1 ELSE 0 END) AS vencendo
                FROM clientes
                """,
                (agora.isoformat(), agora.isoformat(), agora.isoformat(), limite_vencimento.isoformat()),
            ).fetchone()
        total_clientes = int(contagens["total"] or 0)
        clientes_ativos = int(contagens["ativos"] or 0)
        clientes_vencidos = int(contagens["vencidos"] or 0)
        clientes_vencendo = int(contagens["vencendo"] or 0)

    inicio_mes = agora.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    with closing(conectar_banco()) as connection:
        pagamentos = connection.execute(
            """
            SELECT COUNT(CASE WHEN status = 'pending' THEN 1 END) AS pendentes,
                COALESCE(SUM(CASE WHEN status = 'approved' AND ativado_em >= ? THEN valor ELSE 0 END), 0) AS receita_mes
            FROM transacoes
            """,
            (inicio_mes,),
        ).fetchone()
    response.headers["Cache-Control"] = "no-store"
    return {
        "clientes_total": total_clientes,
        "clientes_ativos": clientes_ativos,
        "clientes_vencidos": clientes_vencidos,
        "clientes_vencendo": clientes_vencendo,
        "pagamentos_pendentes": int(pagamentos["pendentes"] or 0),
        "receita_mes": float(pagamentos["receita_mes"] or 0),
        "atualizado_em": agora.isoformat(),
    }


@app.get("/api/admin/clientes")
def listar_clientes_admin(
    response: Response,
    q: str = "",
    situacao: str = "",
    pagina: int = 1,
    por_pagina: int = 50,
    _: None = Depends(autenticar_admin),
):
    garantir_backend_clientes()
    if situacao not in ("", "ativos", "vencidos", "vencendo", "sem-vigencia"):
        raise HTTPException(status_code=422, detail="Filtro de situação inválido.")
    pagina = max(1, pagina)
    por_pagina = max(1, min(100, por_pagina))
    offset = (pagina - 1) * por_pagina
    termo = re.sub(r"[^A-Za-z0-9@._+\-\s]", "", q).strip()[:80]

    if supabase_configurado():
        filtros = filtros_supabase_admin(situacao)
        if termo:
            filtros["or"] = f"(nome.ilike.*{termo}*,email.ilike.*{termo}*,telefone.ilike.*{termo}*)"
        resultado = requisicao_supabase(
            "GET",
            "clientes",
            params={
                "select": "id,nome,telefone,email,marca_tv,created_at,vigencia_ate,telas",
                "order": "created_at.desc",
                "limit": str(por_pagina),
                "offset": str(offset),
                **filtros,
            },
            prefer="count=exact",
        )
        clientes = resultado.get("data", []) if isinstance(resultado, dict) else []
        total = int(resultado.get("total", 0)) if isinstance(resultado, dict) else 0
    else:
        where: list[str] = []
        valores: list[object] = []
        agora = agora_utc().isoformat()
        limite_vencimento = (agora_utc() + timedelta(days=7)).isoformat()
        if situacao == "ativos":
            where.append("vigencia_ate IS NOT NULL AND datetime(vigencia_ate) >= datetime(?)")
            valores.append(agora)
        elif situacao == "vencidos":
            where.append("vigencia_ate IS NOT NULL AND datetime(vigencia_ate) < datetime(?)")
            valores.append(agora)
        elif situacao == "vencendo":
            where.append("vigencia_ate IS NOT NULL AND datetime(vigencia_ate) BETWEEN datetime(?) AND datetime(?)")
            valores.extend((agora, limite_vencimento))
        elif situacao == "sem-vigencia":
            where.append("vigencia_ate IS NULL")
        if termo:
            where.append("(nome LIKE ? OR telefone LIKE ? OR email LIKE ?)")
            valores.extend((f"%{termo}%", f"%{termo}%", f"%{termo}%"))
        condicao = f"WHERE {' AND '.join(where)}" if where else ""
        with closing(conectar_banco()) as connection:
            total = int(connection.execute(f"SELECT COUNT(*) FROM clientes {condicao}", valores).fetchone()[0])
            rows = connection.execute(
                f"SELECT id, nome, telefone, email, marca_tv, criado_em AS created_at, vigencia_ate, telas FROM clientes {condicao} ORDER BY datetime(criado_em) DESC LIMIT ? OFFSET ?",
                (*valores, por_pagina, offset),
            ).fetchall()
        clientes = [dict(row) for row in rows]

    response.headers["Cache-Control"] = "no-store"
    return {"clientes": clientes, "total": total, "pagina": pagina, "por_pagina": por_pagina}


@app.get("/api/admin/pagamentos")
def listar_pagamentos_admin(
    response: Response,
    q: str = "",
    status: str = "",
    pagina: int = 1,
    por_pagina: int = 50,
    _: None = Depends(autenticar_admin),
):
    if status not in ("", "pending", "approved", "rejected", "cancelled", "expired", "refunded", "charged_back"):
        raise HTTPException(status_code=422, detail="Filtro de pagamento inválido.")
    pagina = max(1, pagina)
    por_pagina = max(1, min(100, por_pagina))
    offset = (pagina - 1) * por_pagina
    termo = re.sub(r"[^A-Za-z0-9@._+\-\s]", "", q).strip()[:80]
    where: list[str] = []
    valores: list[object] = []
    if status:
        where.append("t.status = ?")
        valores.append(status)
    if termo:
        where.append("(c.nome LIKE ? OR c.telefone LIKE ? OR t.payment_id_mp LIKE ?)")
        valores.extend((f"%{termo}%", f"%{termo}%", f"%{termo}%"))
    condicao = f"WHERE {' AND '.join(where)}" if where else ""
    consulta = f"""
        FROM transacoes t JOIN clientes c ON c.id = t.cliente_id
        {condicao}
    """
    with closing(conectar_banco()) as connection:
        total = int(connection.execute(f"SELECT COUNT(*) {consulta}", valores).fetchone()[0])
        rows = connection.execute(
            f"""
            SELECT t.payment_id_mp, t.cliente_id, c.nome, c.telefone, t.plano_meses,
                t.telas, t.valor, t.status, t.criado_em, t.expira_em, t.ativado_em
            {consulta}
            ORDER BY datetime(t.criado_em) DESC LIMIT ? OFFSET ?
            """,
            (*valores, por_pagina, offset),
        ).fetchall()
    response.headers["Cache-Control"] = "no-store"
    return {"pagamentos": [dict(row) for row in rows], "total": total, "pagina": pagina, "por_pagina": por_pagina}


@app.get("/api/admin/live21/jobs")
def listar_jobs_live21_admin(
    response: Response,
    status: str = "",
    pagina: int = 1,
    por_pagina: int = 50,
    _: None = Depends(autenticar_admin),
):
    if status not in ("", "queued", "processing", "succeeded", "manual_review", "failed"):
        raise HTTPException(status_code=422, detail="Status de provisionamento inválido.")
    pagina = max(1, pagina)
    por_pagina = max(1, min(100, por_pagina))
    offset = (pagina - 1) * por_pagina
    where = "WHERE j.status = ?" if status else ""
    values: list[object] = [status] if status else []
    with closing(conectar_banco()) as connection:
        total = int(
            connection.execute(
                f"SELECT COUNT(*) FROM live21_jobs j {where}", values
            ).fetchone()[0]
        )
        rows = connection.execute(
            f"""
            SELECT j.id, j.payment_id_mp, j.cliente_id, c.nome, j.plano_meses, j.telas,
                j.expected_expira_em, j.status, j.attempts, j.error_code,
                j.criado_em, j.atualizado_em, j.concluido_em
            FROM live21_jobs j JOIN clientes c ON c.id = j.cliente_id
            {where}
            ORDER BY datetime(j.criado_em) DESC LIMIT ? OFFSET ?
            """,
            (*values, por_pagina, offset),
        ).fetchall()
    response.headers["Cache-Control"] = "no-store"
    return {"jobs": [dict(row) for row in rows], "total": total, "pagina": pagina, "por_pagina": por_pagina}


@app.get("/arte.jpg")
def arte():
    return FileResponse(BASE_DIR / "arte.jpg", media_type="image/jpeg")


@app.get("/api/saude")
def saude():
    backend_configurado = supabase_configurado() if CUSTOMER_DB_BACKEND == "supabase" else True
    return {
        "ok": True,
        "pagamentos_configurados": sdk is not None,
        "clientes_supabase_configurados": supabase_configurado(),
        "clientes_backend": CUSTOMER_DB_BACKEND,
        "clientes_backend_configurado": backend_configurado,
    }


@app.get("/api/configuracao-publica")
def configuracao_publica():
    telefone = carregar_telefone_suporte()
    return {
        "suporte_whatsapp": telefone or None,
        "live21_configurada": False,
    }


@app.post("/api/teste-gratis")
def solicitar_teste_gratis(req: CadastroClienteRequest, background_tasks: BackgroundTasks):
    garantir_backend_clientes()
    modo_teste = LIVE21_INLINE_MODE == "test"
    if not modo_teste:
        raise HTTPException(
            status_code=503,
            detail="O teste grátis automático está desativado. Configure o modo de teste no ambiente do servidor.",
        )
    if not LIVE21_TEST_CUSTOMER_PHONE:
        raise HTTPException(status_code=503, detail="Configure LIVE21_TEST_CUSTOMER_PHONE antes de habilitar o teste no Render.")
    if normalizar_telefone(req.telefone) != normalizar_telefone(LIVE21_TEST_CUSTOMER_PHONE):
        raise HTTPException(status_code=403, detail="O modo de teste aceita somente o telefone allowlisted.")
    if not req.nome.upper().startswith("RPA TESTE") or req.telas != 1:
        raise HTTPException(status_code=422, detail="Use um nome iniciado por RPA TESTE e uma tela no teste Render.")

    cliente = None
    if supabase_configurado():
        cliente = buscar_cliente_supabase(req.telefone)
        if cliente is not None:
            cliente = cachear_cliente_supabase(cliente)
    if cliente is None and not supabase_configurado():
        with closing(conectar_banco()) as connection:
            cliente = connection.execute(
                "SELECT id FROM clientes WHERE telefone = ?", (normalizar_telefone(req.telefone),)
            ).fetchone()
    if cliente is None:
        cliente = cadastrar_cliente(req)
        cliente_id = int(cliente["cliente_id"])
    elif isinstance(cliente, dict):
        cliente_id = int(cliente["id"])
    else:
        cliente_id = int(cliente["id"])

    with closing(conectar_banco()) as connection:
        previous = connection.execute(
            "SELECT payment_id_mp, status FROM live21_jobs WHERE cliente_id = ? AND source = 'trial' ORDER BY id DESC LIMIT 1",
            (cliente_id,),
        ).fetchone()
    if previous and previous["status"] == "succeeded":
        return {"status": "succeeded", "mensagem": "O teste allowlisted já foi provisionado."}
    if previous and previous["status"] in ("queued", "processing"):
        agendar_job_live21_inline(background_tasks, str(previous["payment_id_mp"]))
        return {"status": previous["status"], "mensagem": "O teste já está na fila; não foi criada uma solicitação duplicada."}

    agora = agora_utc()
    expira_em = agora + timedelta(days=30)
    trial_payment_id = f"trial-{uuid.uuid4().hex}"
    with closing(conectar_banco()) as connection:
        connection.execute(
            """
            INSERT INTO transacoes
                (payment_id_mp, cliente_id, plano_meses, telas, valor, status, criado_em, expira_em)
            VALUES (?, ?, 1, 1, 0, 'trial', ?, ?)
            """,
            (trial_payment_id, cliente_id, agora.isoformat(), expira_em.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO live21_jobs
                (payment_id_mp, cliente_id, source, plano_meses, telas, expected_expira_em, status, criado_em, atualizado_em)
            VALUES (?, ?, 'trial', 1, 1, ?, 'queued', ?, ?)
            """,
            (trial_payment_id, cliente_id, expira_em.isoformat(), agora.isoformat(), agora.isoformat()),
        )
        connection.commit()
    agendar_job_live21_inline(background_tasks, trial_payment_id)
    return {"status": "queued", "mensagem": "Pedido de teste enviado para provisionamento."}


@app.get("/api/mercadopago/diagnostico")
def diagnostico_mercadopago():
    if not MP_ACCESS_TOKEN:
        return {
            "conectado": False,
            "mensagem": "Access Token ausente. Configure MERCADOPAGO_TOKEN no arquivo .env e reinicie o servidor.",
        }

    try:
        response = requests.get(
            "https://api.mercadopago.com/users/me",
            headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
            timeout=10,
        )
    except requests.RequestException:
        return {
            "conectado": False,
            "mensagem": "Não foi possível alcançar o Mercado Pago. Verifique a conexão de internet e tente novamente.",
        }

    if response.ok:
        return {"conectado": True, "mensagem": "Access Token aceito pelo Mercado Pago."}
    if response.status_code in (401, 403):
        return {
            "conectado": False,
            "mensagem": "Token recusado. Gere um Access Token novo no Mercado Pago, atualize o .env e reinicie o servidor.",
            "http_status": response.status_code,
        }
    return {
        "conectado": False,
        "mensagem": "O Mercado Pago respondeu com um erro ao validar o Access Token.",
        "http_status": response.status_code,
    }


def validar_assinatura_webhook(signature: str | None, request_id: str | None, data_id: str) -> bool:
    if not MP_WEBHOOK_SECRET or not signature or not request_id:
        return False

    parts = dict(
        item.split("=", 1)
        for item in signature.split(",")
        if "=" in item
    )
    timestamp = parts.get("ts")
    received_hash = parts.get("v1")
    if not timestamp or not received_hash:
        return False

    manifest = f"id:{data_id.lower()};request-id:{request_id};ts:{timestamp};"
    expected_hash = hmac.new(
        MP_WEBHOOK_SECRET.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected_hash, received_hash)


@app.post("/webhook/mercadopago")
async def webhook_mercadopago(request: Request, background_tasks: BackgroundTasks):
    if not MP_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Webhook ainda não configurado no servidor.")

    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}

    event_type = body.get("type") or request.query_params.get("topic")
    if event_type != "payment":
        return {"recebido": True, "ignorado": True}

    query_data_id = request.query_params.get("data.id")
    body_data = body.get("data")
    body_data_id = body_data.get("id") if isinstance(body_data, dict) else None
    data_id = str(query_data_id or "")
    if not data_id:
        raise HTTPException(status_code=400, detail="Notificação sem data.id na query string.")
    if body_data_id is not None and str(body_data_id) != data_id:
        raise HTTPException(status_code=400, detail="O identificador do corpo não corresponde à query.")

    if not validar_assinatura_webhook(
        request.headers.get("x-signature"),
        request.headers.get("x-request-id"),
        data_id,
    ):
        raise HTTPException(status_code=401, detail="Assinatura do webhook inválida.")

    if sdk is None:
        raise HTTPException(status_code=503, detail="Access Token do Mercado Pago não configurado.")

    try:
        payment_response = sdk.payment().get(data_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Não foi possível confirmar o pagamento no Mercado Pago.") from exc

    payment = payment_response.get("response", {})
    if payment_response.get("status") != 200 or not payment.get("id"):
        raise HTTPException(status_code=502, detail="Mercado Pago não confirmou os dados da notificação.")

    atualizado = registrar_status_pagamento(str(payment["id"]), payment)
    if atualizado is None:
        with closing(conectar_banco()) as connection:
            transacao_existe = connection.execute(
                "SELECT 1 FROM transacoes WHERE payment_id_mp = ?",
                (str(payment["id"]),),
            ).fetchone()
        if transacao_existe is None and payment.get("description", "").startswith("PlayTV ") and payment.get("status") == "approved":
            raise HTTPException(status_code=503, detail="Pagamento confirmado antes do registro; o Mercado Pago deve reenviar o webhook.")
        return {"recebido": True, "ignorado": True}

    agendar_job_live21_inline(background_tasks, str(payment["id"]))
    return {"recebido": True}


@app.post("/api/consultar-telefone")
def consultar_telefone(req: ConsultaTelefoneRequest):
    telefone = re.sub(r"\D", "", req.telefone)
    garantir_backend_clientes()
    if supabase_configurado():
        cliente = buscar_cliente_supabase(telefone)
        if cliente is None:
            return {"encontrado": False}
        # Keep a local copy for Mercado Pago payment/transaction linkage; Supabase remains canonical.
        cliente_local = cachear_cliente_supabase(cliente)
        return {"encontrado": True, "cliente": cliente_local}

    with closing(conectar_banco()) as connection:
        cliente = connection.execute(
            "SELECT id, nome, telefone, email, marca_tv, vigencia_ate, telas, supabase_id FROM clientes WHERE telefone = ?",
            (telefone,),
        ).fetchone()

    if cliente is None:
        return {"encontrado": False}
    return {"encontrado": True, "cliente": dict(cliente)}


@app.post("/api/cadastrar-cliente")
def cadastrar_cliente(req: CadastroClienteRequest):
    garantir_backend_clientes()
    if supabase_configurado():
        cliente = salvar_cliente_supabase(req)
        return {"status": "ok", "cliente_id": cliente["id"], "telas": cliente["telas"]}

    agora = datetime.now(timezone.utc).isoformat()
    with closing(conectar_banco()) as connection:
        connection.execute(
            """
            INSERT INTO clientes (nome, telefone, email, marca_tv, criado_em, atualizado_em, telas)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telefone) DO UPDATE SET
                nome = excluded.nome,
                email = excluded.email,
                marca_tv = excluded.marca_tv,
                atualizado_em = excluded.atualizado_em
            """,
            (req.nome, req.telefone, req.email, req.marca_tv, agora, agora, req.telas),
        )
        cliente = connection.execute(
            "SELECT id FROM clientes WHERE telefone = ?", (req.telefone,)
        ).fetchone()
        connection.commit()

    return {"status": "ok", "cliente_id": cliente["id"], "telas": req.telas}


@app.post("/api/gerar-pix")
def gerar_pix(req: GerarPixRequest):
    if sdk is None:
        raise HTTPException(
            status_code=503,
            detail="Configure MERCADOPAGO_TOKEN no arquivo .env para habilitar pagamentos Pix.",
        )
    if req.plano_meses not in PLANOS:
        raise HTTPException(status_code=422, detail="Plano inválido.")

    with closing(conectar_banco()) as connection:
        cliente = connection.execute(
            "SELECT id, nome, email FROM clientes WHERE id = ?", (req.cliente_id,)
        ).fetchone()
    if cliente is None:
        raise HTTPException(status_code=404, detail="Cadastro não encontrado.")

    nome_plano = PLANOS[req.plano_meses]
    valor = round(PRECO_MENSAL * req.plano_meses, 2)
    data_expiracao = agora_utc() + timedelta(minutes=PIX_EXPIRATION_MINUTES)
    data_expiracao_iso = data_expiracao.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    payment_data = {
        "transaction_amount": valor,
        "description": f"PlayTV {nome_plano} — {req.plano_meses} meses — {req.telas} tela(s)",
        "payment_method_id": "pix",
        "payer": {"email": cliente["email"], "first_name": cliente["nome"].split()[0]},
        "external_reference": str(cliente["id"]),
        "date_of_expiration": data_expiracao_iso,
    }
    try:
        payment_response = sdk.payment().create(payment_data)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Não foi possível criar o Pix no Mercado Pago.") from exc

    payment = payment_response.get("response", {})
    transaction_data = payment.get("point_of_interaction", {}).get("transaction_data", {})
    if payment_response.get("status") != 201 or not payment.get("id") or not transaction_data.get("qr_code"):
        response_status = payment_response.get("status", "desconhecido")
        error_code = payment.get("error")
        error_message = payment.get("message")
        causes = payment.get("cause", [])
        mp_reasons = []
        if error_code:
            mp_reasons.append(f"código={str(error_code)[:120]}")
        if error_message:
            mp_reasons.append(f"mensagem={str(error_message)[:300]}")
        if isinstance(causes, list):
            for cause in causes[:2]:
                if isinstance(cause, dict):
                    cause_code = cause.get("code")
                    cause_description = cause.get("description")
                    if cause_code or cause_description:
                        mp_reasons.append(
                            f"causa={str(cause_code or '')[:80]} {str(cause_description or '')[:200]}".strip()
                        )
        logger.warning(
            "Mercado Pago Pix creation failed: HTTP %s; %s",
            response_status,
            " | ".join(mp_reasons) or "no structured error details returned",
        )
        detail = f"Mercado Pago não criou o Pix (HTTP {response_status})."
        if mp_reasons:
            detail += " " + " | ".join(mp_reasons)
        raise HTTPException(status_code=502, detail=detail)

    payment_id = str(payment["id"])
    data_expiracao_mp = payment.get("date_of_expiration") or data_expiracao_iso
    with closing(conectar_banco()) as connection:
        connection.execute(
            """
            INSERT INTO transacoes (payment_id_mp, cliente_id, plano_meses, telas, valor, status, criado_em, expira_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (payment_id, cliente["id"], req.plano_meses, req.telas, valor, payment.get("status", "pending"), agora_utc().isoformat(), data_expiracao_mp),
        )
        connection.commit()

    return {
        "payment_id": payment_id,
        "qr_code": transaction_data["qr_code"],
        "qr_code_base64": transaction_data.get("qr_code_base64"),
        "valor": valor,
        "plano": nome_plano,
        "telas": req.telas,
        "expira_em": data_expiracao_mp,
    }


@app.get("/api/status-pagamento/{payment_id}")
def status_pagamento(payment_id: str, background_tasks: BackgroundTasks):
    with closing(conectar_banco()) as connection:
        transacao = connection.execute(
            """
              SELECT t.status, t.valor, t.plano_meses, t.telas, c.nome, t.expira_em
            FROM transacoes t JOIN clientes c ON c.id = t.cliente_id
            WHERE t.payment_id_mp = ?
            """,
            (payment_id,),
        ).fetchone()
    if transacao is None:
        raise HTTPException(status_code=404, detail="Transação não encontrada.")
    if sdk is None:
        raise HTTPException(status_code=503, detail="Mercado Pago não está configurado.")

    try:
        payment_response = sdk.payment().get(payment_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Não foi possível consultar o pagamento.") from exc
    payment = payment_response.get("response", {})
    if payment_response.get("status") != 200:
        raise HTTPException(status_code=502, detail="O Mercado Pago não retornou o status do pagamento.")

    atualizado = registrar_status_pagamento(payment_id, payment)
    if atualizado is None:
        raise HTTPException(status_code=502, detail="Os dados do pagamento não correspondem à transação registrada.")
    agendar_job_live21_inline(background_tasks, payment_id)

    return {
        "status": atualizado["status"],
        "cliente": {"nome": transacao["nome"]},
        "valor": transacao["valor"],
        "plano_meses": transacao["plano_meses"],
        "telas": transacao["telas"],
        "vigencia_ate": atualizado["vigencia_ate"],
        "expira_em": transacao["expira_em"],
    }