from __future__ import annotations

import html
import os
import re
import shlex
import socket
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Protocol


EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


@dataclass(frozen=True)
class MailMessage:
    to: str
    subject: str
    html: str
    text: str
    idempotency_key: str


@dataclass(frozen=True)
class MailResult:
    status: str
    reference: str = ""
    technical_error: str = ""
    exit_code: int | None = None
    error_type: str = ""
    duration_ms: int = 0


class MailService(Protocol):
    test_mode: bool
    test_recipient: str

    def send(self, message: MailMessage) -> MailResult: ...
    def send_test(self, message: MailMessage) -> MailResult: ...
    def test_connection(self) -> MailResult: ...


class DisabledMailService:
    test_mode = True
    test_recipient = ""

    def send(self, message: MailMessage) -> MailResult:
        return MailResult(status="failed", technical_error="El envío está deshabilitado.", error_type="disabled")

    def send_test(self, message: MailMessage) -> MailResult:
        return self.send(message)

    def test_connection(self) -> MailResult:
        return self.send(MailMessage("", "", "", "", "disabled"))


def validate_email(value: str) -> str:
    normalized = value.strip().casefold()
    if len(normalized) > 254 or not EMAIL_RE.fullmatch(normalized):
        raise ValueError("La dirección de correo no es válida.")
    return normalized


class SshSendmailService:
    """Deliver MIME messages through a pinned SSH host and remote sendmail."""

    STDERR_LIMIT = 2_048

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password_file: str | Path,
        known_hosts_file: str | Path,
        from_address: str,
        from_name: str,
        timeout: float = 30,
        test_mode: bool = True,
        test_recipient: str = "",
        paramiko_module=None,
    ):
        host = host.strip()
        if not host or any(character.isspace() for character in host):
            raise ValueError("El host SSH configurado no es válido.")
        try:
            port_value = int(port)
        except (TypeError, ValueError) as exc:
            raise ValueError("El puerto SSH no es válido.") from exc
        if not 1 <= port_value <= 65535:
            raise ValueError("El puerto SSH no es válido.")
        if not USERNAME_RE.fullmatch(username.strip()):
            raise ValueError("El usuario SSH configurado no es válido.")
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("El timeout de correo debe ser numérico.") from exc
        if not 0 < timeout_value <= 300:
            raise ValueError("El timeout de correo debe estar entre 1 y 300 segundos.")
        from_address = validate_email(from_address)
        if "\r" in from_name or "\n" in from_name or not from_name.strip():
            raise ValueError("El nombre del remitente no es válido.")
        test_recipient = test_recipient.strip().casefold()
        if test_recipient:
            test_recipient = validate_email(test_recipient)
        if test_mode and not test_recipient:
            raise ValueError("Configura un destinatario de prueba antes de habilitar el correo.")

        self.host = host
        self.port = port_value
        self.username = username.strip()
        self.password_file = Path(password_file)
        self.known_hosts_file = Path(known_hosts_file)
        self.from_address = from_address
        self.from_name = from_name.strip()
        self.timeout = timeout_value
        self.test_mode = bool(test_mode)
        self.test_recipient = test_recipient
        self._paramiko_module = paramiko_module

    def validate_configuration(self) -> None:
        self._read_password()
        self._validate_known_hosts_file()
        self._paramiko()

    def _read_password(self) -> str:
        try:
            if not self.password_file.is_file():
                raise ValueError("Falta el archivo privado de contraseña SSH.")
            if self.password_file.stat().st_size > 4_096:
                raise ValueError("El archivo privado de contraseña SSH no es válido.")
            if os.name != "nt" and self.password_file.stat().st_mode & 0o077:
                raise ValueError("El archivo privado de contraseña SSH tiene permisos demasiado amplios.")
            content = self.password_file.read_text(encoding="utf-8")
        except ValueError:
            raise
        except OSError as exc:
            raise ValueError("No se puede leer el archivo privado de contraseña SSH.") from exc
        password = content.rstrip("\r\n")
        if not password:
            raise ValueError("El archivo privado de contraseña SSH está vacío.")
        if "\r" in password or "\n" in password:
            raise ValueError("El archivo privado de contraseña SSH debe contener una sola línea.")
        return password

    def _validate_known_hosts_file(self) -> None:
        try:
            if not self.known_hosts_file.is_file() or self.known_hosts_file.stat().st_size == 0:
                raise ValueError("Falta el archivo privado known_hosts o está vacío.")
            with self.known_hosts_file.open("rb") as handle:
                handle.read(1)
        except ValueError:
            raise
        except OSError as exc:
            raise ValueError("No se puede leer el archivo privado known_hosts.") from exc

    def _paramiko(self):
        if self._paramiko_module is not None:
            return self._paramiko_module
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError("Falta instalar la dependencia Paramiko.") from exc
        return paramiko

    def _connect(self):
        password = self._read_password()
        self._validate_known_hosts_file()
        module = self._paramiko()
        client = module.SSHClient()
        try:
            client.load_host_keys(str(self.known_hosts_file))
            client.set_missing_host_key_policy(module.RejectPolicy())
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=password,
                timeout=self.timeout,
                auth_timeout=self.timeout,
                banner_timeout=self.timeout,
                allow_agent=False,
                look_for_keys=False,
            )
            return client
        except Exception:
            client.close()
            raise

    def _connection_failure(self, exc: Exception, started: float) -> MailResult:
        duration = int((time.monotonic() - started) * 1000)
        if isinstance(exc, (ValueError, RuntimeError)):
            return MailResult("failed", technical_error=str(exc), error_type="configuration_error", duration_ms=duration)
        module = self._paramiko()
        if isinstance(exc, getattr(module, "BadHostKeyException", ())):
            return MailResult("failed", technical_error="La clave del servidor SSH no coincide con known_hosts.", error_type="host_key_mismatch", duration_ms=duration)
        if isinstance(exc, getattr(module, "AuthenticationException", ())):
            return MailResult("failed", technical_error="El servidor SSH rechazó la autenticación.", error_type="authentication_rejected", duration_ms=duration)
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return MailResult("failed", technical_error="La conexión SSH agotó el tiempo de espera.", error_type="connection_timeout", duration_ms=duration)
        if isinstance(exc, getattr(module, "SSHException", ())):
            return MailResult("failed", technical_error="La clave SSH no está autorizada o la conexión fue rechazada.", error_type="ssh_security_error", duration_ms=duration)
        return MailResult("failed", technical_error="No fue posible establecer la conexión SSH.", error_type="connection_error", duration_ms=duration)

    def test_connection(self) -> MailResult:
        started = time.monotonic()
        client = None
        try:
            client = self._connect()
            return MailResult("accepted", reference="ssh-connected", duration_ms=int((time.monotonic() - started) * 1000))
        except Exception as exc:
            return self._connection_failure(exc, started)
        finally:
            if client is not None:
                client.close()

    def send(self, message: MailMessage) -> MailResult:
        return self.send_gift_email([message.to], message.subject, message.html, message.text, message.idempotency_key)

    def send_test(self, message: MailMessage) -> MailResult:
        if not self.test_recipient:
            return MailResult("failed", technical_error="No hay un destinatario de prueba configurado.", error_type="test_recipient_missing")
        return self._deliver(
            [self.test_recipient],
            f"[PRUEBA DE CONEXIÓN] {message.subject}",
            message.html,
            message.text,
            message.idempotency_key,
        )

    def send_gift_email(
        self,
        recipients: list[str],
        subject: str,
        html_body: str,
        text_body: str,
        idempotency_key: str,
    ) -> MailResult:
        if not recipients:
            return MailResult("failed", technical_error="No se proporcionó un destinatario válido.", error_type="invalid_recipient")
        try:
            original = list(dict.fromkeys(validate_email(item) for item in recipients))
        except ValueError as exc:
            return MailResult("failed", technical_error=str(exc), error_type="invalid_recipient")
        if self.test_mode:
            original_label = original[0] if len(original) == 1 else "varios destinatarios"
            subject = f"[PRUEBA - destinatario original: {original_label}] {subject}"
            recipients = [self.test_recipient]
        else:
            recipients = original
        return self._deliver(recipients, subject, html_body, text_body, idempotency_key)

    def _deliver(
        self,
        recipients: list[str],
        subject: str,
        html_body: str,
        text_body: str,
        idempotency_key: str,
    ) -> MailResult:
        if not idempotency_key or len(idempotency_key) > 200 or "\r" in idempotency_key or "\n" in idempotency_key:
            return MailResult("failed", technical_error="La clave de idempotencia no es válida.", error_type="invalid_idempotency_key")
        if "\r" in subject or "\n" in subject:
            return MailResult("failed", technical_error="El asunto del correo no es válido.", error_type="invalid_subject")

        message = EmailMessage()
        message["From"] = formataddr((self.from_name, self.from_address))
        message["To"] = ", ".join(recipients)
        message["Subject"] = subject
        message["X-Idempotency-Key"] = idempotency_key
        message.set_content(text_body or "")
        message.add_alternative(html_body or "", subtype="html")
        mime_bytes = message.as_bytes()
        command = "sendmail -i " + " ".join(shlex.quote(recipient) for recipient in recipients)

        started = time.monotonic()
        client = None
        stdin = None
        command_started = False
        try:
            client = self._connect()
            stdin, stdout, stderr = client.exec_command(command, timeout=self.timeout)
            command_started = True
            stdin.write(mime_bytes)
            stdin.flush()
            stdin.channel.shutdown_write()
            exit_code = stdout.channel.recv_exit_status()
            stderr_raw = stderr.read(self.STDERR_LIMIT + 1)
            if isinstance(stderr_raw, bytes):
                stderr_text = stderr_raw[: self.STDERR_LIMIT].decode("utf-8", errors="replace")
            else:
                stderr_text = str(stderr_raw)[: self.STDERR_LIMIT]
            duration = int((time.monotonic() - started) * 1000)
            if exit_code == 0:
                return MailResult("accepted", reference=idempotency_key, exit_code=0, duration_ms=duration)
            safe_error = " ".join(stderr_text.replace("\x00", "").split())
            for protected in recipients + [self.host, self.username]:
                safe_error = safe_error.replace(protected, "[dato protegido]")
            detail = f"sendmail devolvió código {exit_code}."
            if safe_error:
                detail += f" {safe_error[:300]}"
            return MailResult("failed", technical_error=detail, exit_code=exit_code, error_type="sendmail_exit", duration_ms=duration)
        except Exception as exc:
            if not command_started:
                return self._connection_failure(exc, started)
            return MailResult(
                "unknown",
                technical_error="La ejecución remota terminó sin confirmar el resultado; requiere revisión manual.",
                error_type="remote_result_unknown",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        finally:
            if stdin is not None:
                try:
                    stdin.close()
                except Exception:
                    pass
            if client is not None:
                client.close()


def render_test_mail(*, event_name: str, idempotency_key: str) -> MailMessage:
    safe_event = html.escape(event_name or "Intercambio de Año Nuevo", quote=True)
    html_body = (
        '<!doctype html><html><body style="font-family:Arial,sans-serif;color:#172033">'
        '<table role="presentation" width="100%"><tr><td align="center" style="padding:24px">'
        '<table role="presentation" width="600" style="max-width:600px;background:#ffffff;border:1px solid #dce4df">'
        '<tr><td style="padding:24px;background:#0b6b57;color:#ffffff"><h1>Correo de prueba</h1></td></tr>'
        f'<tr><td style="padding:24px"><p>La integración SSH/sendmail para <strong>{safe_event}</strong> respondió correctamente.</p>'
        '<p>Este mensaje no contiene asignaciones ni preferencias de participantes.</p></td></tr>'
        '</table></td></tr></table></body></html>'
    )
    return MailMessage(
        to="",
        subject=f"Prueba de correo — {event_name or 'Intercambio de Año Nuevo'}",
        html=html_body,
        text=f"Correo de prueba para {event_name or 'Intercambio de Año Nuevo'}. No contiene asignaciones ni preferencias.",
        idempotency_key=idempotency_key,
    )


def render_assignment_mail(
    *,
    giver_name: str,
    recipient_name: str,
    recipient_gifts: str,
    event_name: str,
    instructions: str,
    event_date: str,
    event_time: str,
    location: str,
    budget: str,
    idempotency_key: str,
    to: str,
) -> MailMessage:
    safe = {key: html.escape(value or "", quote=True) for key, value in {
        "giver": giver_name,
        "recipient": recipient_name,
        "gifts": recipient_gifts,
        "event": event_name,
        "instructions": instructions,
        "date": event_date,
        "time": event_time,
        "location": location,
        "budget": budget,
    }.items()}
    details = []
    for label, key in (("Fecha", "date"), ("Hora", "time"), ("Lugar", "location"), ("Presupuesto", "budget")):
        if safe[key]:
            details.append(
                f'<tr><td style="padding:6px 12px;color:#667085">{label}</td>'
                f'<td style="padding:6px 12px;font-weight:700">{safe[key]}</td></tr>'
            )
    details_html = "".join(details) or '<tr><td style="padding:6px 12px">Por confirmar</td></tr>'
    instructions_html = safe["instructions"].replace("\n", "<br>") or "Sigue las indicaciones del equipo organizador."
    gifts_html = safe["gifts"].replace("\n", "<br>")
    html_body = f"""<!doctype html>
<html><body style="margin:0;background:#f4f7fb;font-family:Arial,sans-serif;color:#172033">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f7fb"><tr><td align="center" style="padding:28px 12px">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" style="width:100%;max-width:600px;background:#ffffff;border-radius:16px;overflow:hidden">
<tr><td style="padding:30px;background:#0b6b57;color:#ffffff"><div style="font-size:13px;letter-spacing:1px;text-transform:uppercase">{safe['event']}</div><h1 style="margin:8px 0 0;font-size:28px">Amigo secreto de Año Nuevo</h1></td></tr>
<tr><td style="padding:30px"><p style="font-size:17px">Hola, <strong>{safe['giver']}</strong>.</p><p>Tu amigo secreto es:</p>
<div style="padding:18px;border-radius:12px;background:#fff3d6;font-size:24px;font-weight:700;color:#8a4b08">{safe['recipient']}</div>
<h2 style="font-size:18px;margin-top:28px">Regalos o preferencias</h2><div style="padding:16px;background:#f7f8fa;border-radius:10px;line-height:1.5">{gifts_html}</div>
<h2 style="font-size:18px;margin-top:28px">Datos del intercambio</h2><table role="presentation" cellspacing="0" cellpadding="0">{details_html}</table>
<h2 style="font-size:18px;margin-top:28px">Instrucciones</h2><p style="line-height:1.6">{instructions_html}</p>
<p style="margin-top:28px;padding:14px;border-left:4px solid #ef9f27;background:#fff9ed"><strong>Recuerda:</strong> mantén el nombre en secreto hasta el intercambio.</p></td></tr>
</table></td></tr></table></body></html>"""
    plain_details = "\n".join(
        f"{label}: {value}" for label, value in (
            ("Fecha", event_date), ("Hora", event_time), ("Lugar", location), ("Presupuesto", budget)
        ) if value
    ) or "Datos del intercambio: por confirmar"
    text_body = (
        f"{event_name}\nAmigo secreto de Año Nuevo\n\nHola, {giver_name}.\n\n"
        f"Tu amigo secreto es: {recipient_name}\n\nRegalos o preferencias:\n{recipient_gifts}\n\n"
        f"{plain_details}\n\nInstrucciones:\n{instructions}\n\nMantén el nombre en secreto."
    )
    return MailMessage(
        to=to,
        subject=f"Tu amigo secreto — {event_name}",
        html=html_body,
        text=text_body,
        idempotency_key=idempotency_key,
    )
