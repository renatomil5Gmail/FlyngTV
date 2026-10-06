import importlib
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import main


BASE_DIR = Path(__file__).resolve().parent
PANEL_URL = os.getenv("LIVE21_PANEL_URL", "https://cms.gerencia.ovh").rstrip("/")
PROFILE_DIR = Path(os.getenv("LIVE21_PROFILE_DIR", str(BASE_DIR / ".live21-profile")))
POLL_SECONDS = max(3, int(os.getenv("LIVE21_WORKER_POLL_SECONDS", "10")))
HEADLESS = os.getenv("LIVE21_HEADLESS", "false").strip().lower() in {"1", "true", "yes"}
logger = logging.getLogger("live21_worker")
LOGIN_ATTEMPTED = False


class ManualReviewRequired(Exception):
    pass


def format_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if digits.startswith("55") and len(digits) in (12, 13):
        digits = digits[2:]
    if len(digits) == 11:
        return f"({digits[:2]}) {digits[2:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"({digits[:2]}) {digits[2:6]}-{digits[6:]}"
    return digits


def format_panel_date(value: str) -> str:
    parsed = main.interpretar_data(value)
    if parsed is None:
        raise ManualReviewRequired("invalid_expiration")
    return parsed.strftime("%d-%m-%Y")


def parse_panel_date(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def panel_rows(page, term: str):
    url = f"{PANEL_URL}/gerenciador/usuario-iptv?{urlencode({'termo': term})}"
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.locator("#filtro").wait_for(state="visible", timeout=20000)
    page.wait_for_timeout(700)
    return page.locator("tbody tr").all()


def row_data(row) -> dict | None:
    cells = row.locator("td").all_inner_texts()
    if len(cells) < 7:
        return None
    edit_link = row.locator("a.editar")
    remote = edit_link.get_attribute("data-load-remote") or ""
    match = re.search(r"/(\d+)$", remote)
    return {
        "name": cells[1].strip(),
        "username": cells[2].strip(),
        "password": cells[3].strip(),
        "email": cells[4].strip().lower(),
        "expiration": parse_panel_date(cells[6]),
        "external_id": match.group(1) if match else None,
    }


def find_user(page, term: str, *, username: str | None = None, email: str | None = None, name: str | None = None):
    matches = []
    for row in panel_rows(page, term):
        data = row_data(row)
        if data is None:
            continue
        if username and data["username"] == username:
            matches.append((row, data))
        if email and data["email"] == email.lower() and (not name or data["name"] == name):
            matches.append((row, data))
    unique = {
        match[1]["external_id"] or match[1]["username"] or str(id(match[0])): match
        for match in matches
    }
    if len(unique) > 1:
        raise ManualReviewRequired("multiple_matching_accounts")
    return next(iter(unique.values()), (None, None))


def confirm_swal(page, *, success_contains: str | None = None) -> str:
    popup = page.locator(".swal2-popup:visible")
    popup.wait_for(state="visible", timeout=30000)
    title = (popup.locator(".swal2-title").text_content() or "").strip().lower()
    body = (popup.locator(".swal2-html-container, .swal2-content").first.text_content() or "").strip().lower()
    if "ops" in title or "error" in title:
        popup.locator(".swal2-confirm").evaluate("element => element.click()")
        raise ManualReviewRequired("panel_rejected_operation")
    if success_contains and success_contains.lower() not in f"{title} {body}":
        popup.locator(".swal2-confirm").evaluate("element => element.click()")
        raise ManualReviewRequired("unexpected_panel_response")
    popup.locator(".swal2-confirm").evaluate("element => element.click()")
    return title


def ensure_dashboard(page) -> bool:
    global LOGIN_ATTEMPTED
    if "/gerenciador/" in page.url:
        return True
    if not page.url or page.url == "about:blank":
        page.goto(f"{PANEL_URL}/gerenciador/home", wait_until="domcontentloaded", timeout=45000)
    if "/gerenciador/" in page.url:
        return True

    username = os.getenv("LIVE21_PANEL_USERNAME", "")
    password = os.getenv("LIVE21_PANEL_PASSWORD", "")
    if not username or not password or LOGIN_ATTEMPTED:
        logger.warning("Live21 login required; authenticate in the persistent browser profile.")
        return False

    user_field = page.locator('input[placeholder="Seu usuário"]')
    password_field = page.locator('input[placeholder="Sua senha"]')
    if user_field.count() and password_field.count():
        LOGIN_ATTEMPTED = True
        user_field.fill(username)
        password_field.fill(password)
        page.get_by_role("button", name="Acessar o Painel").click()
        try:
            page.wait_for_url("**/gerenciador/**", timeout=30000)
        except Exception:
            logger.warning("Live21 login needs manual verification; no challenge is bypassed.")
    return "/gerenciador/" in page.url


def create_account(page, job: dict) -> dict:
    client = {key: job[key] for key in ("nome", "email", "telefone")}
    row, existing = find_user(page, client["email"], email=client["email"], name=client["nome"])
    if existing:
        raise ManualReviewRequired("account_appeared_before_create")

    page.locator("button.adicionar").click()
    form = page.locator("#usuarioForm")
    form.wait_for(state="visible", timeout=20000)
    form.locator('[name="nome"]').fill(client["nome"])
    form.locator('[name="email"]').fill(client["email"])
    form.locator('[name="telefone"]').fill(format_phone(client["telefone"]))
    form.locator('[name="expiracao"]').fill(format_panel_date(job["expected_expira_em"]))
    form.locator('[name="valor"]').fill(f"{float(job['valor']):.2f}".replace(".", ","))
    form.evaluate(
        "form => form.querySelectorAll('input[name=\"planos[_ids][]\"]').forEach(input => { input.checked = true; input.dispatchEvent(new Event('change', { bubbles: true })); })"
    )
    if not form.evaluate("element => element.checkValidity()"):
        raise ManualReviewRequired("create_form_invalid")
    page.locator("#salvarAdicionar").evaluate("element => element.click()")
    confirm_swal(page, success_contains="sucesso")
    page.wait_for_function(
        "() => (document.querySelector('#modal_body_add')?.innerText || '').includes('Usuário')",
        timeout=30000,
    )
    result_text = page.locator("#modal_body_add").inner_text()
    username, password = main.extrair_credenciais_live21(result_text)
    if not username or not password:
        raise ManualReviewRequired("created_account_credentials_not_found")
    page.evaluate(
        "() => [...document.querySelectorAll('#m_modal_4 button')].find(button => button.textContent.includes('Finalizado'))?.click()"
    )

    row, created = find_user(page, username, username=username)
    if created is None or not created["external_id"]:
        raise ManualReviewRequired("created_account_row_not_found")
    created["username"] = username
    created["password"] = password
    return created


def renew_account(page, job: dict, account: dict) -> dict:
    row, current = find_user(page, account["usuario"], username=account["usuario"])
    if current is None:
        raise ManualReviewRequired("linked_account_not_found")
    expected = main.interpretar_data(job["expected_expira_em"])
    if expected is None:
        raise ManualReviewRequired("invalid_expected_expiration")
    if current["expiration"] and current["expiration"] >= expected:
        current["username"] = account["usuario"]
        current["password"] = account["senha"]
        return current

    row.locator('input[type="checkbox"]').evaluate("element => { element.checked = true; element.dispatchEvent(new Event('change', { bubbles: true })); }")
    selected = page.locator('tbody input[type="checkbox"]:checked').count()
    if selected != 1:
        raise ManualReviewRequired("renewal_selection_not_unique")
    page.evaluate(
        "() => [...document.querySelectorAll('button')].find(button => button.textContent.includes('Ações'))?.click()"
    )
    page.locator("a.renovaUsersSelecionados").evaluate("element => element.click()")
    popup = page.locator(".swal2-popup:visible")
    popup.wait_for(state="visible", timeout=15000)
    prompt = (popup.inner_text() or "").lower()
    if "1" not in prompt or "30 dias" not in prompt:
        popup.locator(".swal2-cancel").evaluate("element => element.click()")
        raise ManualReviewRequired("renewal_confirmation_mismatch")
    popup.locator(".swal2-confirm").evaluate("element => element.click()")
    confirm_swal(page, success_contains="renovados com sucesso")

    row, renewed = find_user(page, account["usuario"], username=account["usuario"])
    if renewed is None or renewed["expiration"] is None or renewed["expiration"] < expected:
        raise ManualReviewRequired("renewal_expiration_not_verified")
    renewed["username"] = account["usuario"]
    renewed["password"] = account["senha"]
    return renewed


def process_job(page, job: dict) -> None:
    if int(job["plano_meses"]) != 1 or int(job["telas"]) != 1:
        raise ManualReviewRequired("unsupported_plan_or_screen_count")

    account = main.obter_conta_live21_cliente(int(job["cliente_id"]))
    if account:
        result = renew_account(page, job, account)
    else:
        row, existing = find_user(page, job["email"], email=job["email"], name=job["nome"])
        if existing:
            if not existing["external_id"] or not existing["username"] or not existing["password"]:
                raise ManualReviewRequired("matching_account_missing_fields")
            account = {
                "external_id": existing["external_id"],
                "usuario": existing["username"],
                "senha": existing["password"],
                "expira_em": existing["expiration"].isoformat() if existing["expiration"] else None,
            }
            result = renew_account(page, job, account)
        else:
            result = create_account(page, job)
    main.salvar_conta_live21_cliente(
        int(job["cliente_id"]),
        external_id=str(result["external_id"]),
        usuario=str(result["username"]),
        senha=str(result["password"]),
        expira_em=job["expected_expira_em"],
    )
    if job["source"] == "trial":
        main.ativar_vigencia_teste_live21(int(job["cliente_id"]), job["expected_expira_em"])


def process_job_once(job_id: int) -> bool:
    job = main.claim_live21_job(job_id=job_id)
    if job is None:
        return False
    try:
        try:
            sync_playwright = importlib.import_module("playwright.sync_api").sync_playwright
        except ImportError as exc:
            raise ManualReviewRequired("playwright_not_installed") from exc
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=HEADLESS,
                args=["--disable-dev-shm-usage"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                if not ensure_dashboard(page):
                    raise ManualReviewRequired("login_or_challenge_required")
                process_job(page, job)
            finally:
                context.close()
        main.finalizar_live21_job(job_id)
        logger.info("Live21 inline job %s completed.", job_id)
        return True
    except ManualReviewRequired as exc:
        main.finalizar_live21_job(job_id, error_code=str(exc)[:80])
        logger.warning("Live21 inline job %s needs review (%s).", job_id, str(exc)[:80])
        return False
    except Exception as exc:
        main.finalizar_live21_job(job_id, error_code=type(exc).__name__[:80])
        logger.warning("Live21 inline job %s failed (%s).", job_id, type(exc).__name__)
        return False


def run_worker() -> None:
    try:
        sync_playwright = importlib.import_module("playwright.sync_api").sync_playwright
    except ImportError as exc:
        raise RuntimeError("Install requirements-worker.txt and run 'python -m playwright install chromium'.") from exc

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=HEADLESS,
            args=["--disable-dev-shm-usage"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        while True:
            if not ensure_dashboard(page):
                time.sleep(POLL_SECONDS)
                continue
            job = main.claim_live21_job()
            if job is None:
                time.sleep(POLL_SECONDS)
                continue
            try:
                process_job(page, job)
                main.finalizar_live21_job(int(job["id"]))
                logger.info("Live21 job %s completed.", job["id"])
            except ManualReviewRequired as exc:
                main.finalizar_live21_job(int(job["id"]), error_code=str(exc)[:80])
                logger.warning("Live21 job %s needs review (%s).", job["id"], str(exc)[:80])
            except Exception as exc:
                main.finalizar_live21_job(int(job["id"]), error_code=type(exc).__name__[:80])
                logger.warning("Live21 job %s failed (%s).", job["id"], type(exc).__name__)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    run_worker()