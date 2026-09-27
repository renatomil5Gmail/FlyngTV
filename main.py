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
sdk = mercadopago.SDK(MP_ACCESS_TOKEN) if MP_ACCESS_TOKEN else None
PLANOS = {1: ("Básico", 30.00), 3: ("Cinema", 80.00), 6: ("Premium", 150.00)}


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
                vigencia_ate TEXT
            );
            CREATE TABLE IF NOT EXISTS transacoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_id_mp TEXT NOT NULL UNIQUE,
                cliente_id INTEGER NOT NULL REFERENCES clientes(id),
                plano_meses INTEGER NOT NULL,
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
        colunas_transacoes = {
            row["name"] for row in connection.execute("PRAGMA table_info(transacoes)")
        }
        if "expira_em" not in colunas_transacoes:
            connection.execute("ALTER TABLE transacoes ADD COLUMN expira_em TEXT")
        if "ativado_em" not in colunas_transacoes:
            connection.execute("ALTER TABLE transacoes ADD COLUMN ativado_em TEXT")
        connection.commit()


criar_tabelas()


class ConsultaTelefoneRequest(BaseModel):
    telefone: str = Field(min_length=10, max_length=20)


class CadastroClienteRequest(BaseModel):
    nome: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=254)
    telefone: str = Field(min_length=10, max_length=20)
    marca_tv: str = Field(min_length=1, max_length=60)

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
            "SELECT cliente_id, plano_meses, valor, ativado_em, expira_em FROM transacoes WHERE payment_id_mp = ?",
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
                "SELECT vigencia_ate FROM clientes WHERE id = ?",
                (transacao["cliente_id"],),
            ).fetchone()
            if cliente is None:
                return None
            vigencia_atual = interpretar_data(cliente["vigencia_ate"])
            inicio_vigencia = max(agora, vigencia_atual) if vigencia_atual else agora
            vigencia_ate = adicionar_meses(inicio_vigencia, int(transacao["plano_meses"]))
            connection.execute(
                "UPDATE clientes SET vigencia_ate = ?, atualizado_em = ? WHERE id = ?",
                (vigencia_ate.isoformat(), agora.isoformat(), transacao["cliente_id"]),
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
    return {"ok": True, "pagamentos_configurados": sdk is not None}


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
    with closing(conectar_banco()) as connection:
        cliente = connection.execute(
            "SELECT id, nome, telefone, email, marca_tv, vigencia_ate FROM clientes WHERE telefone = ?",
            (telefone,),
        ).fetchone()

    if cliente is None:
        return {"encontrado": False}
    return {"encontrado": True, "cliente": dict(cliente)}


@app.post("/api/cadastrar-cliente")
def cadastrar_cliente(req: CadastroClienteRequest):
    agora = datetime.now(timezone.utc).isoformat()
    with closing(conectar_banco()) as connection:
        connection.execute(
            """
            INSERT INTO clientes (nome, telefone, email, marca_tv, criado_em, atualizado_em)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(telefone) DO UPDATE SET
                nome = excluded.nome,
                email = excluded.email,
                marca_tv = excluded.marca_tv,
                atualizado_em = excluded.atualizado_em
            """,
            (req.nome, req.telefone, req.email, req.marca_tv, agora, agora),
        )
        cliente = connection.execute(
            "SELECT id FROM clientes WHERE telefone = ?", (req.telefone,)
        ).fetchone()
        connection.commit()

    return {"status": "ok", "cliente_id": cliente["id"]}


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

    nome_plano, valor = PLANOS[req.plano_meses]
    data_expiracao = agora_utc() + timedelta(minutes=PIX_EXPIRATION_MINUTES)
    data_expiracao_iso = data_expiracao.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    payment_data = {
        "transaction_amount": valor,
        "description": f"PlayTV {nome_plano} — {req.plano_meses} meses",
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
            INSERT INTO transacoes (payment_id_mp, cliente_id, plano_meses, valor, status, criado_em, expira_em)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (payment_id, cliente["id"], req.plano_meses, valor, payment.get("status", "pending"), agora_utc().isoformat(), data_expiracao_mp),
        )
        connection.commit()

    return {
        "payment_id": payment_id,
        "qr_code": transaction_data["qr_code"],
        "qr_code_base64": transaction_data.get("qr_code_base64"),
        "valor": valor,
        "plano": nome_plano,
        "expira_em": data_expiracao_mp,
    }


@app.get("/api/status-pagamento/{payment_id}")
def status_pagamento(payment_id: str):
    with closing(conectar_banco()) as connection:
        transacao = connection.execute(
            """
              SELECT t.status, t.valor, t.plano_meses, c.nome, t.expira_em
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
        "vigencia_ate": atualizado["vigencia_ate"],
        "expira_em": transacao["expira_em"],
    }