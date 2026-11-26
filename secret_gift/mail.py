from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Protocol


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


class MailService(Protocol):
    def send(self, message: MailMessage) -> MailResult: ...


class DisabledMailService:
    def send(self, message: MailMessage) -> MailResult:
        return MailResult(
            status="failed",
            technical_error="El envío está deshabilitado o falta configurar el adaptador.",
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
