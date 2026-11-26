from __future__ import annotations

import csv
import base64
import hashlib
import hmac
import html
import io
import json
import os
import secrets
import threading
import time
import traceback
import unicodedata
import zipfile
from http import cookies
from pathlib import Path
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

from secret_gift import db
from secret_gift.drawing import DrawError, DrawParticipant, generate_draw
from secret_gift.mail import (
    DisabledMailService,
    MailService,
    SshSendmailService,
    render_assignment_mail,
    render_test_mail,
)


ROOT = Path(__file__).resolve().parent
SESSION_TTL = 8 * 60 * 60


def load_local_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "si", "sí", "on"}


def field_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char)).strip().casefold()


def parse_csv_participants(raw: str, existing_emails: set[str]) -> tuple[list[dict], list[dict]]:
    if len(raw.encode("utf-8")) > 2_000_000:
        raise ValueError("El CSV supera el límite de 2 MB.")
    sample = raw[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(raw), dialect=dialect)
    if not reader.fieldnames:
        raise ValueError("El archivo no contiene encabezados.")
    aliases = {
        "name": {"nombre", "nombre completo", "name", "full name"},
        "email": {"correo", "correo electronico", "email", "e-mail"},
        "gifts": {"regalos", "preferencias", "lista de regalos", "gifts", "gift preferences"},
        "area": {"area", "equipo", "team", "department"},
        "confirmed": {"confirmado", "confirmacion", "participa", "confirmed"},
    }
    normalized_headers = {field_name(header): header for header in reader.fieldnames}
    columns = {}
    for canonical, choices in aliases.items():
        columns[canonical] = next((normalized_headers[item] for item in choices if item in normalized_headers), None)
    missing = [name for name in ("name", "email", "gifts") if not columns[name]]
    if missing:
        raise ValueError("Faltan columnas obligatorias: nombre, correo y regalos.")
    valid, invalid = [], []
    seen = set(existing_emails)
    for line_number, row in enumerate(reader, start=2):
        payload = {
            "name": row.get(columns["name"], ""),
            "email": db.normalize_email(row.get(columns["email"], "")),
            "gifts": row.get(columns["gifts"], ""),
            "area": row.get(columns["area"], "") if columns["area"] else "",
        }
        confirmation = row.get(columns["confirmed"], "") if columns["confirmed"] else "sí"
        if field_name(confirmation) not in {"si", "yes", "true", "1", "confirmado", "confirmada"}:
            invalid.append({"line": line_number, "email": payload["email"], "error": "Participación no confirmada"})
            continue
        if payload["email"] in seen:
            invalid.append({"line": line_number, "email": payload["email"], "error": "Correo duplicado"})
            continue
        try:
            name = db.clean_text(payload["name"])
            area = db.clean_text(payload["area"])
            if len(name) < 2 or "@" not in payload["email"] or len(payload["gifts"].strip()) < 2:
                raise ValueError
            payload.update(name=name, area=area, gifts=payload["gifts"].strip())
        except ValueError:
            invalid.append({"line": line_number, "email": payload["email"], "error": "Datos incompletos o inválidos"})
            continue
        seen.add(payload["email"])
        valid.append(payload)
    return valid, invalid


XLSX_ALIASES = {
    "name": {
        "nombre", "nombre completo", "nombres", "nombres y apellidos", "nombre y apellidos",
        "participante", "name", "full name",
    },
    "email": {"correo", "correo electronico", "email", "e-mail", "mail"},
    "gifts": {
        "regalos", "regalos deseados", "lista de regalos", "preferencias", "preferencias de regalo",
        "que te gustaria recibir", "que regalo te gustaria recibir", "gifts", "gift preferences",
    },
    "area": {"area", "equipo", "gerencia", "departamento", "team", "department"},
    "confirmed": {"confirmado", "confirmacion", "participa", "asistira", "confirmed"},
}


def parse_xlsx_participants(raw: bytes) -> tuple[list[dict], list[dict], str]:
    if not raw or len(raw) > 5_000_000:
        raise ValueError("El Excel está vacío o supera el límite de 5 MB.")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            names = {item.filename for item in entries}
            if len(entries) > 500 or sum(item.file_size for item in entries) > 25_000_000:
                raise ValueError("El Excel descomprimido supera los límites permitidos.")
            if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                raise ValueError("El archivo no es un libro Excel .xlsx válido.")
    except zipfile.BadZipFile as exc:
        raise ValueError("El archivo no es un libro Excel .xlsx válido.") from exc

    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError("Falta instalar openpyxl desde requirements.txt.") from exc

    try:
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError("No se pudo leer el archivo Excel.") from exc

    selected = None
    try:
        for sheet in workbook.worksheets:
            for row_number, row in enumerate(
                sheet.iter_rows(min_row=1, max_row=min(sheet.max_row, 10), values_only=True), start=1
            ):
                normalized = {
                    field_name(str(value)): index
                    for index, value in enumerate(row)
                    if value is not None and str(value).strip()
                }
                columns = {
                    canonical: next((normalized[item] for item in aliases if item in normalized), None)
                    for canonical, aliases in XLSX_ALIASES.items()
                }
                if all(columns[item] is not None for item in ("name", "email", "gifts")):
                    selected = (sheet, row_number, columns)
                    break
            if selected:
                break
        if not selected:
            raise ValueError(
                "No encuentro las columnas obligatorias: Nombre completo, Correo y Regalos."
            )

        sheet, header_row, columns = selected
        valid, invalid, seen = [], [], set()
        max_row = min(sheet.max_row, 10_001)
        for row_number, row in enumerate(
            sheet.iter_rows(min_row=header_row + 1, max_row=max_row, values_only=True),
            start=header_row + 1,
        ):
            if not any(value is not None and str(value).strip() for value in row):
                continue

            def cell(name: str) -> str:
                index = columns.get(name)
                if index is None or index >= len(row) or row[index] is None:
                    return ""
                return str(row[index]).strip()

            payload = {
                "name": db.clean_text(cell("name")),
                "email": db.normalize_email(cell("email")),
                "gifts": cell("gifts"),
                "area": db.clean_text(cell("area")),
            }
            confirmation = field_name(cell("confirmed")) if columns.get("confirmed") is not None else "si"
            source = f"{sheet.title}, fila {row_number}"
            if confirmation not in {"si", "yes", "true", "1", "confirmado", "confirmada", "asistire", "asistira"}:
                invalid.append({"line": row_number, "email": payload["email"], "error": "Participación no confirmada", "source": source})
                continue
            if payload["email"] in seen:
                invalid.append({"line": row_number, "email": payload["email"], "error": "Correo duplicado en el Excel", "source": source})
                continue
            if len(payload["name"]) < 2 or "@" not in payload["email"] or len(payload["gifts"]) < 2:
                invalid.append({"line": row_number, "email": payload["email"], "error": "Datos incompletos o inválidos", "source": source})
                continue
            seen.add(payload["email"])
            valid.append(payload)
        if not valid and not invalid:
            raise ValueError("El Excel no contiene filas de participantes.")
        return valid, invalid, sheet.title
    finally:
        workbook.close()


class Application:
    def __init__(
        self,
        database_path: str | Path,
        admin_password: str,
        *,
        event_name: str = "Intercambio de Año Nuevo",
        mail_service: MailService | None = None,
        mail_enabled: bool = False,
        mail_test_recipient: str = "",
        mail_test_mode: bool = False,
        mail_config_error: str = "",
        mail_max_per_run: int = 100,
    ):
        if not admin_password:
            raise RuntimeError("Configura SECRET_GIFT_ADMIN_PASSWORD antes de iniciar.")
        self.database_path = Path(database_path)
        self.event_id = db.initialize(self.database_path, event_name)
        self.admin_password = admin_password
        self.mail_service = mail_service or DisabledMailService()
        self.mail_enabled = mail_enabled
        self.mail_test_recipient = mail_test_recipient.strip().casefold()
        self.mail_test_mode = mail_test_mode
        self.mail_config_error = mail_config_error
        self.mail_max_per_run = max(1, int(mail_max_per_run))
        self.sessions: dict[str, dict] = {}
        self.send_lock = threading.Lock()

    def session(self, environ) -> tuple[str, dict, bool]:
        jar = cookies.SimpleCookie(environ.get("HTTP_COOKIE", ""))
        sid = jar["sg_session"].value if "sg_session" in jar else ""
        session = self.sessions.get(sid)
        fresh = False
        if not session or time.time() - session["seen"] > SESSION_TTL:
            sid = secrets.token_urlsafe(32)
            session = {"csrf": secrets.token_urlsafe(32), "admin": False, "seen": time.time()}
            self.sessions[sid] = session
            fresh = True
        session["seen"] = time.time()
        return sid, session, fresh

    def __call__(self, environ, start_response):
        sid, session, fresh = self.session(environ)
        try:
            status, headers, body = self.dispatch(environ, session)
        except Exception:
            traceback.print_exc()
            status, headers, body = "500 Internal Server Error", [], self.page("Error", "<div class='notice error'>Ocurrió un error interno.</div>")
        if not any(key.lower() == "content-type" for key, _ in headers):
            headers.append(("Content-Type", "text/html; charset=utf-8"))
        headers.append(("Cache-Control", "no-store"))
        headers.append(("X-Content-Type-Options", "nosniff"))
        headers.append(("X-Frame-Options", "DENY"))
        headers.append(("Referrer-Policy", "no-referrer"))
        headers.append(("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; form-action 'self'; frame-ancestors 'none'"))
        if fresh:
            headers.append(("Set-Cookie", f"sg_session={sid}; Path=/; HttpOnly; SameSite=Strict"))
        start_response(status, headers)
        return [body.encode("utf-8")]

    def dispatch(self, environ, session):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/")
        if path == "/static/style.css" and method == "GET":
            return "200 OK", [("Content-Type", "text/css; charset=utf-8")], (ROOT / "static/style.css").read_text(encoding="utf-8")
        if path == "/static/app.js" and method == "GET":
            return "200 OK", [("Content-Type", "text/javascript; charset=utf-8")], (ROOT / "static/app.js").read_text(encoding="utf-8")
        form = self.read_form(environ) if method == "POST" else {}
        if method == "POST":
            if form.get("_form_error"):
                return self.response("413 Payload Too Large", self.page("Archivo rechazado", "<div class='notice error'>El archivo supera el límite permitido.</div>"))
            token = form.get("csrf", "")
            if not hmac.compare_digest(token, session["csrf"]):
                return self.response("403 Forbidden", self.page("Solicitud rechazada", "<div class='notice error'>Token CSRF inválido.</div>"))
        if path == "/" and method == "GET":
            return self.redirect("/admin")
        if path == "/admin" and method == "GET":
            return self.response("200 OK", self.admin_page(session) if session["admin"] else self.login_page(session))
        if path == "/admin/login" and method == "POST":
            if hmac.compare_digest(form.get("password", ""), self.admin_password):
                session["admin"] = True
                session["csrf"] = secrets.token_urlsafe(32)
                return self.redirect("/admin")
            return self.response("401 Unauthorized", self.login_page(session, "Contraseña incorrecta."))
        if path.startswith("/admin/"):
            if not session["admin"]:
                return self.response("401 Unauthorized", self.login_page(session, "Inicia sesión para continuar."))
            routes = {
                "/admin/logout": self.logout,
                "/admin/event": self.update_event,
                "/admin/import/preview": self.import_preview,
                "/admin/import/commit": self.import_commit,
                "/admin/participant/toggle": self.toggle_participant,
                "/admin/restriction/add": self.add_restriction,
                "/admin/restriction/delete": self.delete_restriction,
                "/admin/draw/preview": self.draw_preview,
                "/admin/draw/cancel": self.cancel_preview,
                "/admin/draw/confirm": self.confirm_draw,
                "/admin/mail/send": self.send_pending,
                "/admin/mail/retry": self.retry_failed,
                "/admin/mail/test": self.send_test_mail,
                "/admin/mail/connection": self.test_mail_connection,
            }
            handler = routes.get(path)
            if handler and method == "POST":
                return handler(session, form)
        return self.response("404 Not Found", self.page("No encontrado", "<p>La página no existe.</p>"))

    def read_form(self, environ) -> dict[str, str]:
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length > 8_000_000:
            environ["wsgi.input"].read(min(length, 8_000_000))
            return {"_form_error": "too_large"}
        raw = environ["wsgi.input"].read(length).decode("utf-8", errors="replace")
        return {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}

    def response(self, status, body, headers=None):
        return status, headers or [], body

    def redirect(self, location: str):
        return "303 See Other", [("Location", location)], ""

    def page(self, title: str, content: str, admin: bool = False) -> str:
        nav = "<a href='/admin'>Panel privado</a>"
        return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)} · Amigo secreto</title><link rel="stylesheet" href="/static/style.css"></head><body><header><a class="brand" href="/admin"><span>AS</span><strong>Amigo secreto</strong></a><nav>{nav}</nav></header><main>{content}</main><footer>Portal privado de organización del intercambio.</footer><script src="/static/app.js"></script></body></html>"""

    def public_page(self, session, message: str = "") -> str:
        with db.connect(self.database_path) as conn:
            event = conn.execute("SELECT * FROM events WHERE id=?", (self.event_id,)).fetchone()
            count = conn.execute("SELECT COUNT(*) AS n FROM participants WHERE event_id=? AND active=1", (self.event_id,)).fetchone()["n"]
        open_for_registration = event["status"] == "open"
        notice = f"<div class='notice success'>{html.escape(message)}</div>" if message else ""
        form = f"""<form method="post" action="/register" class="card form-grid"><input type="hidden" name="csrf" value="{session['csrf']}"><label>Nombre completo<input name="name" autocomplete="name" required minlength="2"></label><label>Correo electrónico<input name="email" type="email" autocomplete="email" required></label><label class="full">Regalos o preferencias<textarea name="gifts" rows="4" required minlength="2" placeholder="Ideas, tallas, colores, pasatiempos…"></textarea></label><label>Área o equipo <span class="muted">(opcional)</span><input name="area"></label><label class="checkbox"><input type="checkbox" name="confirmed" value="yes" required> Confirmo mi participación</label><button class="primary full" type="submit">Confirmar inscripción</button></form>""" if open_for_registration else "<div class='card closed'><h2>Inscripciones cerradas</h2><p>El equipo organizador ya está preparando el sorteo.</p></div>"
        content = f"""<section class="hero"><div><p class="eyebrow">{html.escape(event['name'])}</p><h1>Un regalo pensado.<br><em>Un secreto bien guardado.</em></h1><p>Registra tus datos y preferencias. Solo el equipo organizador tendrá acceso a la información del sorteo.</p><div class="count"><strong>{count}</strong><span>personas inscritas</span></div></div><div>{notice}{form}</div></section>"""
        return self.page("Inscripción", content)

    def register(self, session, form):
        if form.get("confirmed") != "yes":
            return self.response("400 Bad Request", self.public_page(session, "Debes confirmar tu participación."))
        with db.connect(self.database_path) as conn:
            event = conn.execute("SELECT status FROM events WHERE id=?", (self.event_id,)).fetchone()
            if event["status"] != "open":
                return self.response("409 Conflict", self.public_page(session, "Las inscripciones están cerradas."))
            try:
                db.add_participant(conn, self.event_id, name=form.get("name", ""), email=form.get("email", ""), gifts=form.get("gifts", ""), area=form.get("area", ""))
            except ValueError as exc:
                return self.response("400 Bad Request", self.public_page(session, str(exc)))
        return self.response("201 Created", self.public_page(session, "¡Inscripción confirmada! Ya estás participando."))

    def login_page(self, session, error: str = "") -> str:
        alert = f"<div class='notice error'>{html.escape(error)}</div>" if error else ""
        return self.page("Acceso administrativo", f"""<section class="narrow"><p class="eyebrow">Acceso restringido</p><h1>Panel administrativo</h1>{alert}<form class="card" method="post" action="/admin/login"><input type="hidden" name="csrf" value="{session['csrf']}"><label>Contraseña de administración<input type="password" name="password" required autocomplete="current-password"></label><button class="primary" type="submit">Ingresar</button></form></section>""")

    def admin_page(self, session, message: str = "") -> str:
        with db.connect(self.database_path) as conn:
            event = conn.execute("SELECT * FROM events WHERE id=?", (self.event_id,)).fetchone()
            participants = conn.execute("SELECT * FROM participants WHERE event_id=? ORDER BY name", (self.event_id,)).fetchall()
            restrictions = conn.execute("""SELECT r.id,g.name giver_name,p.name recipient_name FROM restrictions r JOIN participants g ON g.id=r.giver_id JOIN participants p ON p.id=r.recipient_id WHERE r.event_id=? ORDER BY g.name,p.name""", (self.event_id,)).fetchall()
            statuses = {row["mail_status"]: row["n"] for row in conn.execute("SELECT mail_status,COUNT(*) n FROM assignments WHERE event_id=? GROUP BY mail_status", (self.event_id,))}
            mail_rows = conn.execute("""SELECT a.mail_status,a.attempts,a.technical_error,g.name giver_name,g.email FROM assignments a JOIN participants g ON g.id=a.giver_id WHERE a.event_id=? ORDER BY g.name""", (self.event_id,)).fetchall()
        active = [p for p in participants if p["active"] and p["confirmed"]]
        message_lower = message.casefold()
        success_prefixes = (
            "vista previa completa", "sorteo confirmado", "proceso finalizado", "lista cargada",
            "la conexión ssh fue validada", "el servidor sendmail aceptó",
        )
        error_markers = ("error", "falta", "no ", "rechaz", "inválid", "debes", "corrige", "agotó", "deshabilitad", "incierto")
        alert_class = "error" if any(item in message_lower for item in error_markers) and not message_lower.startswith(success_prefixes) else "success"
        alert = f"<div class='notice {alert_class}'>{html.escape(message)}</div>" if message else ""
        csrf = session["csrf"]
        test_nonce = session.setdefault("mail_test_nonce", secrets.token_urlsafe(24))
        status_label = {"open": "Lista editable", "preview": "Sorteo preparado", "confirmed": "Sorteo confirmado"}[event["status"]]
        participant_rows = "".join(f"""<tr><td><strong>{html.escape(p['name'])}</strong><small>{html.escape(p['area'])}</small></td><td>{html.escape(p['email'])}</td><td class="clip">{html.escape(p['gifts'])}</td><td><span class="pill {'ok' if p['active'] else 'off'}">{'Activo' if p['active'] else 'Excluido'}</span></td><td><form method="post" action="/admin/participant/toggle"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="id" value="{p['id']}"><button class="text" {'disabled' if event['status'] != 'open' else ''}>Cambiar</button></form></td></tr>""" for p in participants) or "<tr><td colspan='5'>Aún no hay participantes.</td></tr>"
        people_options = "".join(f"<option value='{p['id']}'>{html.escape(p['name'])} — {html.escape(p['email'])}</option>" for p in active)
        restriction_rows = "".join(f"<li>{html.escape(r['giver_name'])} no puede regalar a {html.escape(r['recipient_name'])}<form method='post' action='/admin/restriction/delete'><input type='hidden' name='csrf' value='{csrf}'><input type='hidden' name='id' value='{r['id']}'><button class='text'>Quitar</button></form></li>" for r in restrictions) or "<li class='muted'>Sin bloqueos específicos.</li>"
        mail_status_rows = "".join(f"<tr><td>{html.escape(r['giver_name'])}<small>{html.escape(r['email'])}</small></td><td><span class='pill {r['mail_status']}'>{self.mail_status_label(r['mail_status'])}</span></td><td>{r['attempts']}</td><td>{html.escape(r['technical_error'][:120])}</td></tr>" for r in mail_rows)
        assignment_count = sum(statuses.values())
        pending_count = statuses.get("pending", 0)
        connection_status = session.get("mail_connection_status", "No probada")
        draw_actions = ""
        if event["status"] == "open":
            draw_actions = f"<form method='post' action='/admin/draw/preview'><input type='hidden' name='csrf' value='{csrf}'><button class='primary'>Generar vista previa del sorteo</button></form>"
        elif event["status"] == "preview":
            draw_actions = f"""<div class="notice warn">La combinación completa fue validada. Todavía no se ha guardado definitivamente ni se han enviado correos.</div><div class="actions"><form method="post" action="/admin/draw/cancel"><input type="hidden" name="csrf" value="{csrf}"><button>Cancelar vista previa</button></form><form method="post" action="/admin/draw/confirm" data-confirm="Confirmar el sorteo es definitivo. ¿Continuar?"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="confirmation" value="CONFIRMAR"><button class="danger">Confirmar sorteo</button></form></div>"""
        else:
            test_completed = bool(event["email_test_succeeded_at"])
            batch_enabled = self.mail_enabled and test_completed
            if self.mail_config_error:
                disabled_note = f"<div class='notice error'>{html.escape(self.mail_config_error)}</div>"
            elif not self.mail_enabled:
                disabled_note = "<div class='notice warn'>El envío real está deshabilitado. Completa la configuración privada y reinicia la aplicación.</div>"
            elif not test_completed:
                disabled_note = "<div class='notice warn'>Realiza correctamente al menos un correo de prueba antes de procesar la cola.</div>"
            else:
                disabled_note = ""
            mode_notice = (
                f"<div class='notice warn'><strong>Modo de prueba:</strong> todos los mensajes se sustituirán por {html.escape(self.mail_test_recipient)}.</div>"
                if self.mail_test_mode
                else "<div class='notice error'><strong>Modo real activo:</strong> los mensajes se enviarán a los participantes.</div>"
            )
            send_summary = f"""<div class="send-summary"><p><strong>{len(active)}</strong> participantes · <strong>{assignment_count}</strong> asignaciones válidas · <strong>{pending_count}</strong> pendientes</p><p>Conexión SSH: <strong>{html.escape(connection_status)}</strong></p>{mode_notice}</div>"""
            draw_actions = f"""{disabled_note}{send_summary}<div class="actions"><form method="post" action="/admin/mail/send"><input type="hidden" name="csrf" value="{csrf}"><label>Escribe ENVIAR CORREOS para confirmar<input name="confirmation" autocomplete="off" required></label><button class="primary" {'disabled' if not batch_enabled else ''}>Enviar correos pendientes</button></form><form method="post" action="/admin/mail/retry"><input type="hidden" name="csrf" value="{csrf}"><label>Escribe ENVIAR CORREOS para reintentar<input name="confirmation" autocomplete="off" required></label><button {'disabled' if not batch_enabled else ''}>Reintentar solo fallidos</button></form></div>"""
        test_enabled = self.mail_enabled and bool(self.mail_test_recipient)
        mail_state = "Configuración válida" if self.mail_enabled else "Envío deshabilitado"
        if self.mail_config_error:
            mail_state = html.escape(self.mail_config_error)
        test_note = "El mensaje de prueba solo se enviará al destinatario configurado."
        if not self.mail_test_recipient:
            test_note = "Configura SECRET_GIFT_TEST_RECIPIENT para habilitar la prueba."
        if self.mail_test_mode and self.mail_test_recipient:
            test_note += f" Destino actual: {html.escape(self.mail_test_recipient)}."
        mail_config_panel = f"""<section class="card"><p class="eyebrow">Integración SSH / sendmail</p><h2>Validar conexión</h2><p>{mail_state}</p><p>Estado de conexión: <strong>{html.escape(connection_status)}</strong></p><p class="muted">{test_note}</p><div class="actions"><form method="post" action="/admin/mail/connection"><input type="hidden" name="csrf" value="{csrf}"><button {'disabled' if not self.mail_enabled else ''}>Probar conexión SSH</button></form><form method="post" action="/admin/mail/test"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="test_nonce" value="{test_nonce}"><button {'disabled' if not test_enabled else ''}>Enviar correo de prueba</button></form></div></section>"""
        content = f"""<section class="admin-head"><div><p class="eyebrow">Portal privado</p><h1>{html.escape(event['name'])}</h1><span class="pill ok">{status_label}</span></div><form method="post" action="/admin/logout"><input type="hidden" name="csrf" value="{csrf}"><button>Salir</button></form></section>{alert}
<section class="metrics"><div><strong>{len(active)}</strong><span>participantes cargados</span></div><div><strong>{sum(statuses.values())}</strong><span>asignaciones confirmadas</span></div><div><strong>{pending_count}</strong><span>correos pendientes</span></div><div><strong>{statuses.get('accepted',0)}</strong><span>aceptados por sendmail</span></div></section>
<section class="card step"><p class="eyebrow">Paso 1</p><h2>Cargar participantes desde Excel</h2><p class="muted">El Excel debe incluir Nombre completo, Correo y Regalos. Área o equipo es opcional.</p><form method="post" action="/admin/import/preview" id="xlsx-upload-form"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="xlsx_base64"><input type="hidden" name="filename"><label>Archivo Excel (.xlsx)<input id="xlsx-file" type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" required></label><button class="primary" {'disabled' if event['status'] != 'open' else ''}>Revisar Excel</button></form>{self.import_summary(session, csrf)}</section>
<section class="card"><div class="section-title"><div><p class="eyebrow">Lista cargada</p><h2>Participantes que entrarán al sorteo</h2></div></div><div class="table-wrap"><table><thead><tr><th>Persona</th><th>Correo</th><th>Preferencias</th><th>Estado</th><th></th></tr></thead><tbody>{participant_rows}</tbody></table></div></section>
<details class="card"><summary>Datos del evento</summary><form class="form-grid top" method="post" action="/admin/event"><input type="hidden" name="csrf" value="{csrf}"><label>Nombre<input name="name" value="{html.escape(event['name'], quote=True)}" required></label><label>Fecha<input name="event_date" value="{html.escape(event['event_date'], quote=True)}"></label><label>Hora<input name="event_time" value="{html.escape(event['event_time'], quote=True)}"></label><label>Lugar<input name="location" value="{html.escape(event['location'], quote=True)}"></label><label>Presupuesto<input name="budget" value="{html.escape(event['budget'], quote=True)}"></label><label class="full">Instrucciones<textarea name="instructions">{html.escape(event['instructions'])}</textarea></label><label class="checkbox full"><input type="checkbox" name="avoid_same_area" value="1" {'checked' if event['avoid_same_area'] else ''}> Evitar asignaciones dentro de la misma área</label><button {'disabled' if event['status'] != 'open' else ''}>Guardar datos</button></form></details>
<details class="card"><summary>Restricciones opcionales</summary><form class="form-grid top" method="post" action="/admin/restriction/add"><input type="hidden" name="csrf" value="{csrf}"><label>Quien regala<select name="giver_id">{people_options}</select></label><label>No puede regalar a<select name="recipient_id">{people_options}</select></label><button {'disabled' if event['status'] != 'open' else ''}>Agregar restricción</button></form><ul class="restriction-list">{restriction_rows}</ul></details>
<section class="card draw step"><p class="eyebrow">Paso 2</p><h2>Sortear</h2><p>El sistema valida el sorteo completo, pero nunca muestra quién le tocó a quién.</p>{draw_actions}</section>
<section class="step"><p class="eyebrow">Paso 3</p>{mail_config_panel}</section>
{f'<section class="card"><p class="eyebrow">Resultado de envíos</p><h2>Estado por participante</h2><p class="muted">La tabla muestra estados, nunca la persona asignada. “Aceptado” significa que sendmail devolvió código 0.</p><div class="table-wrap"><table><thead><tr><th>Participante</th><th>Estado</th><th>Intentos</th><th>Error técnico</th></tr></thead><tbody>{mail_status_rows}</tbody></table></div></section>' if mail_rows else ''}"""
        return self.page("Administración", content, admin=True)

    def import_summary(self, session, csrf):
        preview = session.get("import_preview")
        if not preview:
            return ""
        invalid = "".join(f"<li>{html.escape(row.get('source') or ('Fila ' + str(row['line'])))}: {html.escape(row['email'])} — {html.escape(row['error'])}</li>" for row in preview["invalid"])
        can_import = len(preview["valid"]) >= 2 and not preview["invalid"]
        note = "" if can_import else "<div class='notice warn'>Corrige las filas inválidas. Se necesitan al menos dos participantes válidos.</div>"
        return f"""<div class="import-result"><p><strong>{html.escape(preview['filename'])}</strong> · Hoja {html.escape(preview['sheet'])}</p><strong>{len(preview['valid'])} válidos · {len(preview['invalid'])} inválidos</strong>{f'<ul>{invalid}</ul>' if invalid else ''}{note}<form method="post" action="/admin/import/commit" data-confirm="La lista actual será reemplazada por este Excel. ¿Continuar?"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="token" value="{preview['token']}"><button class="primary" {'disabled' if not can_import else ''}>Usar esta lista de participantes</button></form></div>"""

    @staticmethod
    def mail_status_label(status):
        return {"pending": "Pendiente", "sending": "Procesando", "accepted": "Aceptado por servidor", "failed": "Fallido", "unknown": "Incierto"}.get(status, status)

    def logout(self, session, form):
        session.clear(); session.update(csrf=secrets.token_urlsafe(32), admin=False, seen=time.time())
        return self.redirect("/admin")

    def update_event(self, session, form):
        with db.connect(self.database_path) as conn:
            event = conn.execute("SELECT status FROM events WHERE id=?", (self.event_id,)).fetchone()
            if event["status"] != "open":
                return self.response("409 Conflict", self.admin_page(session, "La configuración queda bloqueada al generar la vista previa."))
            conn.execute("""UPDATE events SET name=?,instructions=?,event_date=?,event_time=?,location=?,budget=?,avoid_same_area=? WHERE id=?""", (db.clean_text(form.get("name", "")) or "Intercambio de Año Nuevo", form.get("instructions", "").strip(), form.get("event_date", "").strip(), form.get("event_time", "").strip(), db.clean_text(form.get("location", "")), db.clean_text(form.get("budget", "")), 1 if form.get("avoid_same_area") == "1" else 0, self.event_id))
        return self.redirect("/admin")

    def import_preview(self, session, form):
        filename = Path(form.get("filename", "")).name
        if not filename.casefold().endswith(".xlsx"):
            return self.response("400 Bad Request", self.admin_page(session, "Selecciona un archivo Excel con extensión .xlsx."))
        try:
            raw = base64.b64decode(form.get("xlsx_base64", ""), validate=True)
            valid, invalid, sheet = parse_xlsx_participants(raw)
        except (ValueError, RuntimeError) as exc:
            return self.response("400 Bad Request", self.admin_page(session, str(exc)))
        session["import_preview"] = {
            "token": secrets.token_urlsafe(24),
            "valid": valid,
            "invalid": invalid,
            "filename": filename,
            "sheet": sheet,
        }
        return self.redirect("/admin")

    def import_commit(self, session, form):
        preview = session.get("import_preview")
        if not preview or not hmac.compare_digest(form.get("token", ""), preview["token"]):
            return self.response("409 Conflict", self.admin_page(session, "La vista previa de importación ya no es válida."))
        if len(preview["valid"]) < 2 or preview["invalid"]:
            return self.response("409 Conflict", self.admin_page(session, "Corrige el Excel antes de reemplazar la lista."))
        imported = 0
        with db.connect(self.database_path) as conn:
            event = conn.execute("SELECT status FROM events WHERE id=?", (self.event_id,)).fetchone()
            if not event or event["status"] != "open":
                return self.response("409 Conflict", self.admin_page(session, "No se puede reemplazar la lista después de preparar el sorteo."))
            with db.transaction(conn):
                conn.execute("DELETE FROM restrictions WHERE event_id=?", (self.event_id,))
                conn.execute("DELETE FROM participants WHERE event_id=?", (self.event_id,))
                for item in preview["valid"]:
                    db.add_participant(conn, self.event_id, **item)
                    imported += 1
                conn.execute("INSERT INTO audit_log(event_id,action,actor,technical_detail,created_at) VALUES(?,?,?,?,?)", (self.event_id, "participants_replaced_from_xlsx", "admin", f"count={imported}", db.now()))
        session.pop("import_preview", None)
        return self.response("200 OK", self.admin_page(session, f"Lista cargada: {imported} participantes listos para el sorteo."))

    def toggle_participant(self, session, form):
        with db.connect(self.database_path) as conn:
            conn.execute("UPDATE participants SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=? AND event_id=? AND (SELECT status FROM events WHERE id=?)='open'", (form.get("id", ""), self.event_id, self.event_id))
        return self.redirect("/admin")

    def add_restriction(self, session, form):
        giver, recipient = form.get("giver_id", ""), form.get("recipient_id", "")
        if not giver or not recipient or giver == recipient:
            return self.response("400 Bad Request", self.admin_page(session, "Selecciona dos personas distintas."))
        with db.connect(self.database_path) as conn:
            try:
                conn.execute("INSERT INTO restrictions(id,event_id,giver_id,recipient_id,created_at) SELECT ?,?,?,?,? WHERE (SELECT status FROM events WHERE id=?)='open'", (secrets.token_hex(16), self.event_id, giver, recipient, db.now(), self.event_id))
            except Exception:
                return self.response("409 Conflict", self.admin_page(session, "La restricción ya existe o no es válida."))
        return self.redirect("/admin")

    def delete_restriction(self, session, form):
        with db.connect(self.database_path) as conn:
            conn.execute("DELETE FROM restrictions WHERE id=? AND event_id=? AND (SELECT status FROM events WHERE id=?)='open'", (form.get("id", ""), self.event_id, self.event_id))
        return self.redirect("/admin")

    def draw_preview(self, session, form):
        with db.connect(self.database_path) as conn:
            rows = conn.execute("SELECT id,area FROM participants WHERE event_id=? AND active=1 AND confirmed=1", (self.event_id,)).fetchall()
            blocked = {(row["giver_id"], row["recipient_id"]) for row in conn.execute("SELECT giver_id,recipient_id FROM restrictions WHERE event_id=?", (self.event_id,))}
            event = conn.execute("SELECT avoid_same_area FROM events WHERE id=?", (self.event_id,)).fetchone()
            try:
                result = generate_draw([DrawParticipant(row["id"], row["area"]) for row in rows], blocked, bool(event["avoid_same_area"]))
                digest = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
                db.save_preview(conn, self.event_id, result, digest, "admin")
            except DrawError as exc:
                return self.response("422 Unprocessable Entity", self.admin_page(session, str(exc)))
        return self.response("200 OK", self.admin_page(session, "Vista previa completa y válida generada. No se enviaron correos."))

    def cancel_preview(self, session, form):
        with db.connect(self.database_path) as conn:
            conn.execute("UPDATE events SET status='open',preview_json=NULL,preview_hash=NULL WHERE id=? AND status='preview'", (self.event_id,))
            conn.execute("INSERT INTO audit_log(event_id,action,actor,created_at) VALUES(?,?,?,?)", (self.event_id, "draw_preview_cancelled", "admin", db.now()))
        return self.redirect("/admin")

    def confirm_draw(self, session, form):
        if form.get("confirmation") != "CONFIRMAR":
            return self.response("400 Bad Request", self.admin_page(session, "Falta la confirmación explícita."))
        try:
            with db.connect(self.database_path) as conn:
                db.confirm_preview(conn, self.event_id, "admin")
        except DrawError as exc:
            return self.response("409 Conflict", self.admin_page(session, str(exc)))
        return self.response("200 OK", self.admin_page(session, "Sorteo confirmado y guardado definitivamente. Aún no se enviaron correos."))

    def send_pending(self, session, form):
        return self.process_mail(session, form, retry_failed=False)

    def retry_failed(self, session, form):
        return self.process_mail(session, form, retry_failed=True)

    def test_mail_connection(self, session, form):
        if not self.mail_enabled:
            return self.response("503 Service Unavailable", self.admin_page(session, "La integración SSH está deshabilitada o incompleta."))
        if not self.send_lock.acquire(blocking=False):
            return self.response("409 Conflict", self.admin_page(session, "Ya hay una operación de correo en curso."))
        try:
            result = self.mail_service.test_connection()
            if result.status == "accepted":
                session["mail_connection_status"] = "Exitosa"
                message = "La conexión SSH fue validada correctamente. Todavía no se envió ningún correo."
                status = "200 OK"
            else:
                session["mail_connection_status"] = "Error"
                message = result.technical_error or "No fue posible validar la conexión SSH."
                status = "502 Bad Gateway"
            with db.connect(self.database_path) as conn:
                conn.execute(
                    "INSERT INTO audit_log(event_id,action,actor,technical_detail,created_at) VALUES(?,?,?,?,?)",
                    (
                        self.event_id,
                        "ssh_connection_test",
                        "admin",
                        f"duration_ms={result.duration_ms};status={result.status};error_type={result.error_type}",
                        db.now(),
                    ),
                )
            return self.response(status, self.admin_page(session, message))
        finally:
            self.send_lock.release()

    def send_test_mail(self, session, form):
        if not self.mail_enabled:
            return self.response("503 Service Unavailable", self.admin_page(session, "El envío está deshabilitado o su configuración no es válida."))
        if not self.mail_test_recipient:
            return self.response("409 Conflict", self.admin_page(session, "No hay un destinatario de prueba configurado."))
        expected_nonce = session.get("mail_test_nonce", "")
        submitted_nonce = form.get("test_nonce", "")
        if not expected_nonce or not hmac.compare_digest(expected_nonce, submitted_nonce):
            return self.response("409 Conflict", self.admin_page(session, "Esta solicitud de prueba ya fue utilizada o expiró."))
        session["mail_test_nonce"] = secrets.token_urlsafe(24)
        if not self.send_lock.acquire(blocking=False):
            return self.response("409 Conflict", self.admin_page(session, "Ya hay un envío en curso."))
        try:
            with db.connect(self.database_path) as conn:
                event = conn.execute("SELECT name FROM events WHERE id=?", (self.event_id,)).fetchone()
            message = render_test_mail(
                event_name=event["name"],
                idempotency_key=f"test:{self.event_id}:{submitted_nonce}",
            )
            sender = getattr(self.mail_service, "send_test", None)
            result = None
            if sender is None:
                result_status, result_error = "failed", "El adaptador no admite el modo de prueba."
            else:
                try:
                    result = sender(message)
                    result_status = result.status if result.status in {"accepted", "failed", "unknown"} else "unknown"
                    result_error = result.technical_error[:500]
                except Exception:
                    result_status, result_error = "failed", "Ocurrió un fallo interno en el adaptador de correo."
            with db.connect(self.database_path) as conn:
                conn.execute(
                    "INSERT INTO audit_log(event_id,action,actor,technical_detail,created_at) VALUES(?,?,?,?,?)",
                    (
                        self.event_id,
                        "test_mail_attempt",
                        "admin",
                        f"duration_ms={getattr(result, 'duration_ms', 0)};exit_code={getattr(result, 'exit_code', None)};status={result_status};error_type={getattr(result, 'error_type', 'adapter_error')}",
                        db.now(),
                    ),
                )
            if result_status == "accepted":
                session["mail_connection_status"] = "Exitosa; sendmail aceptó la prueba"
                with db.connect(self.database_path) as conn:
                    conn.execute(
                        "UPDATE events SET email_test_succeeded_at=? WHERE id=?",
                        (db.now(), self.event_id),
                    )
                return self.response("200 OK", self.admin_page(session, "El servidor sendmail aceptó el correo de prueba."))
            if result_status == "unknown":
                return self.response("202 Accepted", self.admin_page(session, "El resultado del correo de prueba es incierto. No lo reenvíes automáticamente."))
            return self.response("502 Bad Gateway", self.admin_page(session, result_error or "El servidor rechazó el correo de prueba."))
        finally:
            self.send_lock.release()

    def process_mail(self, session, form, retry_failed: bool):
        if not self.mail_enabled:
            return self.response("503 Service Unavailable", self.admin_page(session, "El envío está deshabilitado."))
        if form.get("confirmation", "").strip() != "ENVIAR CORREOS":
            return self.response("400 Bad Request", self.admin_page(session, "Escribe exactamente ENVIAR CORREOS para confirmar."))
        if not self.send_lock.acquire(blocking=False):
            return self.response("409 Conflict", self.admin_page(session, "Ya hay un envío en curso."))
        try:
            target = "failed" if retry_failed else "pending"
            with db.connect(self.database_path) as conn:
                event = conn.execute("SELECT * FROM events WHERE id=? AND status='confirmed'", (self.event_id,)).fetchone()
                if not event:
                    return self.response("409 Conflict", self.admin_page(session, "Confirma el sorteo antes de enviar."))
                if not event["email_test_succeeded_at"]:
                    return self.response("409 Conflict", self.admin_page(session, "Debes completar correctamente un correo de prueba antes de procesar la cola."))
                rows = conn.execute("""SELECT a.*,g.name giver_name,g.email,r.name recipient_name,r.gifts recipient_gifts FROM assignments a JOIN participants g ON g.id=a.giver_id JOIN participants r ON r.id=a.recipient_id WHERE a.event_id=? AND a.mail_status=? ORDER BY g.name LIMIT ?""", (self.event_id, target, self.mail_max_per_run)).fetchall()
                assignment_ids = [row["id"] for row in rows]
                with db.transaction(conn):
                    for assignment_id in assignment_ids:
                        conn.execute("UPDATE assignments SET mail_status='sending',attempts=attempts+1,last_attempt_at=?,technical_error='' WHERE id=? AND mail_status=?", (db.now(), assignment_id, target))
            for row in rows:
                message = render_assignment_mail(giver_name=row["giver_name"], recipient_name=row["recipient_name"], recipient_gifts=row["recipient_gifts"], event_name=event["name"], instructions=event["instructions"], event_date=event["event_date"], event_time=event["event_time"], location=event["location"], budget=event["budget"], idempotency_key=row["idempotency_key"], to=row["email"])
                result = None
                try:
                    result = self.mail_service.send(message)
                    status = result.status if result.status in {"accepted", "failed", "unknown"} else "unknown"
                    error = result.technical_error[:500]
                    reference = result.reference[:200]
                except TimeoutError:
                    status, error, reference = "unknown", "Timeout: resultado incierto; requiere revisión manual.", ""
                except Exception:
                    status, error, reference = "failed", "Ocurrió un fallo interno en el adaptador de correo.", ""
                with db.connect(self.database_path) as conn:
                    conn.execute("UPDATE assignments SET mail_status=?,provider_reference=?,technical_error=? WHERE id=? AND mail_status='sending'", (status, reference, error, row["id"]))
                    result_exit_code = getattr(result, "exit_code", None)
                    result_duration = getattr(result, "duration_ms", 0)
                    result_error_type = getattr(result, "error_type", "adapter_error")
                    conn.execute(
                        "INSERT INTO audit_log(event_id,action,actor,technical_detail,created_at) VALUES(?,?,?,?,?)",
                        (
                            self.event_id,
                            "mail_attempt",
                            "admin",
                            f"participant={row['giver_id']};duration_ms={result_duration};exit_code={result_exit_code};status={status};error_type={result_error_type}",
                            db.now(),
                        ),
                    )
            return self.response("200 OK", self.admin_page(session, f"Proceso finalizado: {len(rows)} correo(s) procesado(s)."))
        finally:
            self.send_lock.release()


def main() -> None:
    load_local_env(ROOT / ".env")
    password = os.getenv("SECRET_GIFT_ADMIN_PASSWORD", "")
    database_setting = Path(os.getenv("SECRET_GIFT_DB", "data/secret_gift.sqlite3"))
    database_path = database_setting if database_setting.is_absolute() else ROOT / database_setting
    mail_enabled_requested = env_flag("SECRET_GIFT_EMAIL_ENABLED")
    mail_test_mode = env_flag("SECRET_GIFT_TEST_MODE", default=True)
    mail_test_recipient = os.getenv("SECRET_GIFT_TEST_RECIPIENT", "").strip()
    password_file_setting = Path(os.getenv("SECRET_GIFT_SSH_PASSWORD_FILE", "secrets/secret_gift_ssh_password.txt"))
    password_file = password_file_setting if password_file_setting.is_absolute() else ROOT / password_file_setting
    known_hosts_setting = Path(os.getenv("SECRET_GIFT_SSH_KNOWN_HOSTS", "secrets/secret_gift_known_hosts"))
    known_hosts_file = known_hosts_setting if known_hosts_setting.is_absolute() else ROOT / known_hosts_setting
    mail_service: MailService = DisabledMailService()
    mail_enabled = False
    mail_config_error = ""
    if mail_enabled_requested:
        try:
            mail_service = SshSendmailService(
                host=os.getenv("SECRET_GIFT_SSH_HOST", "172.19.30.62"),
                port=os.getenv("SECRET_GIFT_SSH_PORT", "22"),
                username=os.getenv("SECRET_GIFT_SSH_USER", "usr_mds"),
                password_file=password_file,
                known_hosts_file=known_hosts_file,
                from_address=os.getenv("SECRET_GIFT_EMAIL_FROM", ""),
                from_name=os.getenv("SECRET_GIFT_EMAIL_FROM_NAME", "Amigo secreto de Año Nuevo"),
                timeout=os.getenv("SECRET_GIFT_EMAIL_TIMEOUT", "30"),
                test_mode=mail_test_mode,
                test_recipient=mail_test_recipient,
            )
            mail_service.validate_configuration()
            mail_enabled = True
        except (ValueError, RuntimeError) as exc:
            mail_config_error = str(exc)
    try:
        mail_max_per_run = int(os.getenv("SECRET_GIFT_EMAIL_MAX_PER_RUN", "100"))
    except ValueError:
        mail_max_per_run = 100
    app = Application(
        database_path,
        password,
        event_name=os.getenv("SECRET_GIFT_EVENT_NAME", "Intercambio de Año Nuevo"),
        mail_service=mail_service,
        mail_enabled=mail_enabled,
        mail_test_recipient=mail_test_recipient,
        mail_test_mode=mail_test_mode,
        mail_config_error=mail_config_error,
        mail_max_per_run=mail_max_per_run,
    )
    initial_values = {
        "event_date": os.getenv("SECRET_GIFT_EVENT_DATE", ""),
        "location": os.getenv("SECRET_GIFT_EVENT_LOCATION", ""),
        "budget": os.getenv("SECRET_GIFT_BUDGET", ""),
    }
    with db.connect(app.database_path) as conn:
        for column, value in initial_values.items():
            if value:
                conn.execute(
                    f"UPDATE events SET {column}=? WHERE id=? AND {column}=''",
                    (value, app.event_id),
                )
    host = os.getenv("SECRET_GIFT_HOST", "127.0.0.1")
    port = int(os.getenv("SECRET_GIFT_PORT", "8000"))
    print(f"Aplicación disponible en http://{host}:{port}")
    with make_server(host, port, app) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
