from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from html import escape as html_escape
from typing import Protocol

from app.core.config import EmailSettings, get_email_settings

logger = logging.getLogger(__name__)


class EmailSender(Protocol):
    """Anything able to deliver a plain-text + HTML message to one address."""

    def send(self, *, to: str, subject: str, text: str, html: str) -> None: ...


class SmtpEmailSender:
    """Delivers through any SMTP provider (Gmail, Outlook, SendGrid, Mailgun...)."""

    def __init__(self, settings: EmailSettings) -> None:
        self._settings = settings

    def send(self, *, to: str, subject: str, text: str, html: str) -> None:
        settings = self._settings
        if not settings.smtp_host or not settings.sender:
            raise RuntimeError("SMTP_HOST and EMAIL_FROM must be configured to send email")

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = settings.sender
        message["To"] = to
        message.set_content(text)
        message.add_alternative(html, subtype="html")

        context = ssl.create_default_context()
        if settings.smtp_use_ssl:
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, timeout=15, context=context
            )
        else:
            smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
        with smtp:
            if not settings.smtp_use_ssl and settings.smtp_use_starttls:
                smtp.starttls(context=context)
            if settings.smtp_username and settings.smtp_password:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)


class ConsoleEmailSender:
    """Local development only: prints the message instead of sending it."""

    def send(self, *, to: str, subject: str, text: str, html: str) -> None:
        print(f"[EMAIL_BACKEND=console] To: {to}\nSubject: {subject}\n\n{text}", flush=True)


def get_email_sender() -> EmailSender:
    settings = get_email_settings()
    if settings.backend == "console":
        return ConsoleEmailSender()
    return SmtpEmailSender(settings)


def deliver_safely(sender: EmailSender, *, to: str, subject: str, text: str, html: str) -> None:
    """Run as a background task: a mail failure must never reach the client,
    otherwise the response would reveal whether the address is registered."""
    try:
        sender.send(to=to, subject=subject, text=text, html=html)
    except Exception:
        logger.exception("email %r could not be delivered", subject)


def password_reset_message(*, nombre: str, code: str, minutes: int) -> tuple[str, str, str]:
    subject = "SportMatch: código para restablecer tu contraseña"
    text = (
        f"Hola {nombre},\n\n"
        "Recibimos una solicitud para restablecer la contraseña de tu cuenta SportMatch.\n\n"
        f"Tu código es: {code}\n\n"
        f"Ingresa este código en la app. Vence en {minutes} minutos y solo puede usarse una vez.\n\n"
        "Si no solicitaste este cambio, ignora este correo: tu contraseña actual sigue funcionando.\n"
    )
    html = (
        '<div style="font-family:Arial,sans-serif;max-width:480px;margin:auto">'
        "<h2>Restablecer contraseña</h2>"
        f"<p>Hola {html_escape(nombre)},</p>"
        "<p>Recibimos una solicitud para restablecer la contraseña de tu cuenta SportMatch.</p>"
        '<p style="font-size:28px;font-weight:bold;letter-spacing:6px;text-align:center">'
        f"{code}</p>"
        f"<p>Ingresa este código en la app. Vence en {minutes} minutos y solo puede usarse "
        "una vez.</p>"
        '<p style="color:#666;font-size:12px">Si no solicitaste este cambio, ignora este '
        "correo: tu contraseña actual sigue funcionando.</p></div>"
    )
    return subject, text, html


def email_verification_message(*, nombre: str, code: str, minutes: int) -> tuple[str, str, str]:
    subject = "SportMatch: código para verificar tu correo"
    text = (
        f"Hola {nombre},\n\n"
        "Gracias por registrarte en SportMatch. Para activar tu cuenta, verifica tu correo.\n\n"
        f"Tu código es: {code}\n\n"
        f"Ingresa este código en la app. Vence en {minutes} minutos y solo puede usarse una vez. "
        "Si vence, puedes pedir uno nuevo desde la app.\n\n"
        "Si no creaste una cuenta en SportMatch, ignora este correo.\n"
    )
    html = (
        '<div style="font-family:Arial,sans-serif;max-width:480px;margin:auto">'
        "<h2>Verifica tu correo</h2>"
        f"<p>Hola {html_escape(nombre)},</p>"
        "<p>Gracias por registrarte en SportMatch. Para activar tu cuenta, verifica tu correo.</p>"
        '<p style="font-size:28px;font-weight:bold;letter-spacing:6px;text-align:center">'
        f"{code}</p>"
        f"<p>Ingresa este código en la app. Vence en {minutes} minutos y solo puede usarse "
        "una vez. Si vence, puedes pedir uno nuevo desde la app.</p>"
        '<p style="color:#666;font-size:12px">Si no creaste una cuenta en SportMatch, '
        "ignora este correo.</p></div>"
    )
    return subject, text, html
