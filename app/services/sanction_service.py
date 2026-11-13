"""HU-45: suspender o sancionar cuentas.

Como se aplica la restriccion de acceso (todas leen `sancion`, nada depende de un job ni del token):
  * login y emision de token (Ms_Users): `UserService.login` / `_issue_token` llaman a `ensure_not_suspended`.
  * cada peticion que llega a Ms_Users con un token existente: `get_current_user` (api/dependencies.py).
  * el resto de servicios y el chat: el gateway pregunta `GET /internal/v1/accounts/{id}/status` (api/internal.py)
    con cache corta; ver Ms_gateway/gateway/app/services/account_guard.py.
Una temporal "vence sola": estar en vigor es `estado = 'activa' AND (fin IS NULL OR fin > ahora)`.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import HTTPException, status

from app.repositories.base import (
    SanctionConflictError,
    SanctionRecord,
    SanctionReportError,
    SanctionTargetNotFoundError,
    StoredUser,
    UserRepository,
    display_name,
)
from app.schemas.sanction import (
    AccountStatus,
    SanctionCreate,
    SanctionLift,
    SanctionListQuery,
    SanctionPerson,
    SanctionRead,
)
from app.services.email_sender import EmailSender

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    """The clock of sanctions. Tests replace it to simulate that time has passed."""
    return datetime.now(timezone.utc)


def suspension_message(tipo: str, motivo: str, fin: datetime | None) -> str:
    if tipo == "permanente" or fin is None:
        return f"Tu cuenta fue suspendida de forma permanente. Motivo: {motivo}"
    return f"Tu cuenta está suspendida hasta el {fin.astimezone(timezone.utc):%d-%m-%Y}. Motivo: {motivo}"


class AccountSuspendedError(Exception):
    """403 account_suspended: carries only what the person may see (never who reported or who sanctioned)."""

    def __init__(self, tipo: str, motivo: str, fin: datetime | None) -> None:
        super().__init__("account suspended")
        self.tipo, self.motivo, self.fin = tipo, motivo, fin

    def body(self) -> dict:
        return {"detail": suspension_message(self.tipo, self.motivo, self.fin), "code": "account_suspended",
                "tipo": self.tipo, "motivo": self.motivo, "fin": None if self.fin is None else self.fin.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}


def ensure_not_suspended(repository: UserRepository, user_id: UUID) -> None:
    sanction = repository.get_active_sanction(user_id, utcnow())
    if sanction is not None:
        raise AccountSuspendedError(sanction.tipo, sanction.motivo, sanction.fin)


def is_suspended(repository: UserRepository, user_id: UUID) -> bool:
    """True while the account has a sanction in force (used to leave it out of what is offered to others)."""
    return repository.get_active_sanction(user_id, utcnow()) is not None


class SanctionService:
    def __init__(self, repository: UserRepository, email_sender: EmailSender | None = None) -> None:
        self._repository = repository
        self._email_sender = email_sender

    # -- admin API ----------------------------------------------------------------------------

    def apply(self, admin: StoredUser, payload: SanctionCreate) -> SanctionRead:
        self._require_admin(admin)
        target = self._repository.get_user(payload.usuario_id)
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Usuario no encontrado.")
        if target.id == admin.id or target.role == "admin":
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Esta cuenta no puede ser sancionada.")
        now = utcnow()
        fin = now + timedelta(days=payload.dias) if payload.tipo == "temporal" else None
        try:
            record = self._repository.apply_sanction(
                sanction_id=uuid4(), user_id=target.id, tipo=payload.tipo, dias=payload.dias, motivo=payload.motivo,
                admin_id=admin.id, report_id=payload.reporte_id, now=now, fin=fin)
        except SanctionConflictError:
            raise HTTPException(status.HTTP_409_CONFLICT, "La cuenta ya tiene una sanción vigente.") from None
        except SanctionTargetNotFoundError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Usuario no encontrado.") from None
        except SanctionReportError as error:
            raise _report_problem(error.reason) from None
        self._notify(target, record, applied=True)
        return self._as_read(record, now)

    def lift(self, admin: StoredUser, sanction_id: UUID, payload: SanctionLift) -> SanctionRead:
        self._require_admin(admin)
        now = utcnow()
        result = self._repository.lift_sanction(sanction_id, admin.id, payload.motivo, now)
        if result is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Sanción no encontrada.")
        record, changed = result
        if changed:
            target = self._repository.get_user(record.usuario_id)
            if target is not None:
                self._notify(target, record, applied=False)
        return self._as_read(record, now)

    def list(self, admin: StoredUser, query: SanctionListQuery) -> list[SanctionRead]:
        self._require_admin(admin)
        now = utcnow()
        records = self._repository.list_sanctions(
            query.estado, query.usuario_id, query.limit, (query.pagina - 1) * query.limit, now)
        return [self._as_read(record, now) for record in records]

    def get(self, admin: StoredUser, sanction_id: UUID) -> SanctionRead:
        self._require_admin(admin)
        record = self._repository.get_sanction(sanction_id)
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Sanción no encontrada.")
        return self._as_read(record, utcnow())

    # -- internal: what the gateway asks -----------------------------------------------------------

    def account_status(self, user_id: UUID) -> AccountStatus:
        sanction = self._repository.get_active_sanction(user_id, utcnow())
        if sanction is None:
            return AccountStatus(suspended=False)
        return AccountStatus(suspended=True, tipo=sanction.tipo, motivo=sanction.motivo, fin=sanction.fin)

    # -- helpers --------------------------------------------------------------------------------

    def _require_admin(self, user: StoredUser) -> None:
        if user.role != "admin":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Solo un administrador puede gestionar sanciones.")

    def _person(self, user_id: UUID | None) -> SanctionPerson | None:
        if user_id is None:
            return None
        user = self._repository.get_user(user_id)
        return SanctionPerson(
            user_id=user_id, nombre=None if user is None else user.profile.nombre,
            apellido_inicial=None if user is None else f"{user.profile.apellido_paterno[:1].upper()}.")

    def _as_read(self, record: SanctionRecord, now: datetime) -> SanctionRead:
        return SanctionRead(
            id=record.id, usuario=self._person(record.usuario_id), tipo=record.tipo, motivo=record.motivo,
            inicio=record.inicio, fin=record.fin, estado=record.state_at(now), reporte_id=record.reporte_id,
            admin=self._person(record.admin_id), levantada_por=self._person(record.levantada_por),
            levantada_en=record.levantada_en, motivo_levantamiento=record.motivo_levantamiento,
            fecha_creacion=record.fecha_creacion)

    def _notify(self, target: StoredUser, record: SanctionRecord, *, applied: bool) -> None:
        """The sanction is already saved: a failing notice is logged loudly but never undoes it."""
        if applied:
            titulo = "Tu cuenta fue suspendida"
            mensaje = suspension_message(record.tipo, record.motivo, record.fin)
        else:
            titulo = "Se levantó la suspensión de tu cuenta"
            mensaje = f"Ya puedes volver a usar SportMatch. Motivo: {record.motivo_levantamiento}"
        data = {"sancion_id": str(record.id), "tipo": record.tipo, "motivo": record.motivo,
                "fin": None if record.fin is None else record.fin.isoformat(),
                "evento": "aplicada" if applied else "levantada"}
        try:
            self._repository.add_notification(target.id, "sancion", titulo, mensaje, data)
        except Exception:
            logger.exception("sanction %s: the notice could not be saved", record.id)
        if self._email_sender is None:
            logger.error("sanction %s: no email sender configured, the person was not emailed", record.id)
            return
        try:
            self._email_sender.send(to=target.email, subject=titulo, text=f"Hola {target.profile.nombre},\n\n{mensaje}\n",
                                    html=f"<p>Hola {html.escape(target.profile.nombre)},</p><p>{html.escape(mensaje)}</p>")
        except Exception:
            logger.exception("sanction %s: the email could not be delivered", record.id)


def _report_problem(reason: str) -> HTTPException:
    if reason == "not_escalated":
        return HTTPException(status.HTTP_409_CONFLICT, "El reporte debe estar escalado para originar una sanción.")
    if reason == "other_target":
        return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "El reporte no corresponde a esa cuenta.")
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "El reporte indicado no existe.")
