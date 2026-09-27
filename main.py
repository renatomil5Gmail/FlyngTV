import os
import re
import sqlite3
import hashlib
import hmac
import logging
import calendar
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mercadopago
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
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
MP_ACCESS_TOKEN = os.getenv("MERCADOPAGO_TOKEN")
MP_WEBHOOK_SECRET = os.getenv("MERCADOPAGO_WEBHOOK_SECRET")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
CUSTOMER_DB_BACKEND = os.getenv("CUSTOMER_DB_BACKEND", "sqlite").strip().lower()
sdk = mercadopago.SDK(MP_ACCESS_TOKEN) if MP_ACCESS_TOKEN else None
PRECO_MENSAL_POR_TELA = 35.00
PLANOS = {1: "Básico", 3: "Cinema", 6: "Premium"}


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
            """
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
        raise HTTPException(
            status_code=503,
            detail="Não foi possível consultar ou salvar o cadastro no Supabase. Tente novamente ou fale com o suporte.",
        ) from exc
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="O Supabase retornou uma resposta inválida.") from exc


def buscar_cliente_supabase(telefone: str) -> dict | None:
    rows = requisicao_supabase(
        "GET",
        "clientes",
        params={"select": "*", "telefone": f"eq.{telefone}", "limit": "1"},
    )
    if not isinstance(rows, list) or not rows:
        return None
    return rows[0] if isinstance(rows[0], dict) else None


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
        return digits


class GerarPixRequest(BaseModel):
    cliente_id: int = Field(gt=0)
    plano_meses: int
    telas: int = Field(default=1, ge=1, le=4)


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
            vigencia_ate = adicionar_meses(inicio_vigencia, int(transacao["plano_meses"]))
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


@app.get("/", response_class=HTMLResponse)
def pagina_inicial():
    return HTMLResponse((BASE_DIR / "index.html").read_text(encoding="utf-8"))


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
def solicitar_teste_gratis(req: CadastroClienteRequest):
    garantir_backend_clientes()
    if supabase_configurado():
        cliente = buscar_cliente_supabase(req.telefone)
    else:
        with closing(conectar_banco()) as connection:
            cliente = connection.execute(
                "SELECT id FROM clientes WHERE telefone = ?", (req.telefone,)
            ).fetchone()
    if cliente is not None:
        raise HTTPException(
            status_code=409,
            detail="Este WhatsApp já possui cadastro. Consulte seu cadastro ou fale com o suporte para solicitar o teste.",
        )
    raise HTTPException(
        status_code=503,
        detail="Ainda não é possível liberar o teste automaticamente porque falta configurar a integração oficial da Live21. Nenhum cadastro de teste foi criado. Fale com o suporte.",
    )


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
async def webhook_mercadopago(request: Request):
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
    valor = round(PRECO_MENSAL_POR_TELA * req.plano_meses * req.telas, 2)
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
def status_pagamento(payment_id: str):
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

    return {
        "status": atualizado["status"],
        "cliente": {"nome": transacao["nome"]},
        "valor": transacao["valor"],
        "plano_meses": transacao["plano_meses"],
        "telas": transacao["telas"],
        "vigencia_ate": atualizado["vigencia_ate"],
        "expira_em": transacao["expira_em"],
    }