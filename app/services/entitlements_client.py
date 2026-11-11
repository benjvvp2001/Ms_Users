"""Beneficios por plan (HU-54): cliente pequeño de ms-pago y control de límites.

Copia idéntica en ms-machine, Ms_activities y Ms-club (cada servicio es un repositorio independiente).
Los límites viven solo en ms-pago (GET /internal/v1/entitlements/{usuario|club}/{id}); aquí solo se cuenta.

- Caché en memoria por cuenta (ENTITLEMENTS_CACHE_SECONDS, 60 s por defecto).
- Fail-open: si ms-pago no responde o no está configurado, la acción se permite y se registra un aviso.
  Una caída de pagos nunca debe bloquear a un usuario Pro.
- Al llegar al límite: HTTP 402 {codigo: "LIMITE_PLAN", recurso, limite, usado, se_libera_en, detail}.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx

logger = logging.getLogger(__name__)

CODIGO = "LIMITE_PLAN"
DOWN_BACKOFF_SECONDS = 10.0
_TIPOS = ("usuario", "club")
_MENSAJES = {
    "solicitudes_match": "solicitudes de match",
    "postulaciones_actividades": "postulaciones a actividades",
    "postulaciones_convocatorias": "postulaciones a convocatorias",
    "actividades_organizadas": "actividades organizadas",
    "convocatorias_abiertas": "convocatorias abiertas",
    "comunicados": "comunicados",
    "canales": "canales",
}


class LimitePlanError(Exception):
    """El plan gratuito no permite más de esta acción por ahora. main.py lo convierte en HTTP 402."""

    status_code = 402

    def __init__(self, recurso: str, limite: int, usado: int, se_libera_en: datetime | None,
                 ventana_horas: int | None) -> None:
        super().__init__(recurso)
        self.recurso, self.limite, self.usado = recurso, limite, usado
        self.se_libera_en, self.ventana_horas = se_libera_en, ventana_horas

    def body(self) -> dict[str, Any]:
        que = _MENSAJES.get(self.recurso, self.recurso)
        if self.ventana_horas == 24:
            cuando = " cada 24 h"
        elif self.ventana_horas:
            cuando = f" cada {self.ventana_horas // 24} días" if self.ventana_horas % 24 == 0 else \
                f" cada {self.ventana_horas} h"
        else:
            cuando = " a la vez"
        return {
            "codigo": CODIGO,
            "recurso": self.recurso,
            "limite": self.limite,
            "usado": self.usado,
            "se_libera_en": self.se_libera_en.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            if self.se_libera_en else None,
            "detail": f"Te quedan 0 de {self.limite} {que}{cuando} en el plan gratuito. Pásate a Pro para "
                      "no tener límite.",
        }


def limite_plan_handler(_request, error: LimitePlanError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=402, content=error.body(), headers={"Cache-Control": "no-store"})


@dataclass(frozen=True)
class EntitlementsSettings:
    url: str
    token: str | None
    timeout_seconds: float = 2.0
    cache_seconds: float = 60.0


def settings_from_env() -> EntitlementsSettings | None:
    """PAYMENTS_SERVICE_URL vacío: sin control de límites (se avisa una vez en el log)."""
    url = os.getenv("PAYMENTS_SERVICE_URL", "").strip().rstrip("/")
    if not url:
        logger.warning("PAYMENTS_SERVICE_URL is not configured: plan limits are not enforced (fail-open)")
        return None
    if not url.startswith(("http://", "https://")):
        raise RuntimeError("PAYMENTS_SERVICE_URL must be an http(s) URL")

    def number(name: str, default: str) -> float:
        try:
            value = float(os.getenv(name, default))
        except ValueError as error:
            raise RuntimeError(f"{name} must be a number") from error
        if not math.isfinite(value) or value < 0:
            raise RuntimeError(f"{name} must be zero or greater")
        return value

    timeout = number("PAYMENTS_TIMEOUT_SECONDS", "2") or 2.0
    return EntitlementsSettings(url, os.getenv("INTERNAL_SERVICE_TOKEN") or None, timeout,
                                number("ENTITLEMENTS_CACHE_SECONDS", "60"))


class EntitlementsClient:
    """GET /internal/v1/entitlements/{tipo}/{id} de ms-pago, con caché. None = desconocido (fail-open)."""

    def __init__(self, settings: EntitlementsSettings, client: httpx.Client | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._settings = settings
        self._client = client or httpx.Client(base_url=settings.url, timeout=settings.timeout_seconds,
                                              trust_env=False)
        self._owns_client = client is None
        self._clock = clock
        self._cache: dict[tuple[str, UUID], tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()
        # After a connection error or timeout, skip ms-pago for a few seconds instead of
        # making every action wait for the timeout (still fail-open).
        self._down_until = 0.0

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _headers(self) -> dict[str, str]:
        return {"X-Internal-Service-Token": self._settings.token or ""}

    def _cached(self, key: tuple[str, UUID]) -> dict[str, Any] | None:
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > self._clock():
                return hit[1]
        return None

    def _store(self, key: tuple[str, UUID], value: dict[str, Any]) -> None:
        with self._lock:
            if len(self._cache) > 10_000:
                self._cache.clear()
            self._cache[key] = (self._clock() + self._settings.cache_seconds, value)

    def get(self, tipo: str, cuenta_id: UUID) -> dict[str, Any] | None:
        if tipo not in _TIPOS:
            raise ValueError(tipo)
        key = (tipo, cuenta_id)
        cached = self._cached(key)
        if cached is not None:
            return cached
        if not self._settings.token:
            logger.warning("INTERNAL_SERVICE_TOKEN is not configured: plan limits are not enforced (fail-open)")
            return None
        if self._clock() < self._down_until:
            return None
        try:
            response = self._client.get(f"/internal/v1/entitlements/{tipo}/{cuenta_id}", headers=self._headers())
            if response.status_code != 200:
                logger.warning("ms-pago answered %s for the plan of %s %s: allowing the action (fail-open)",
                               response.status_code, tipo, cuenta_id)
                return None
            value = response.json()
            if not isinstance(value, dict) or not isinstance(value.get("limites"), dict):
                raise ValueError("unexpected entitlements body")
        except (httpx.HTTPError, ValueError) as error:
            if isinstance(error, httpx.TransportError):
                self._down_until = self._clock() + DOWN_BACKOFF_SECONDS
            logger.warning("ms-pago is not available for the plan of %s %s: allowing the action (fail-open): %s",
                           tipo, cuenta_id, error)
            return None
        self._store(key, value)
        return value

    def many(self, tipo: str, ids: Iterable[UUID]) -> dict[UUID, dict[str, Any]]:
        """Varias cuentas (para marcar destacados). Las que no se pueden saber no aparecen."""
        result: dict[UUID, dict[str, Any]] = {}
        missing: list[UUID] = []
        for cuenta_id in dict.fromkeys(ids):
            cached = self._cached((tipo, cuenta_id))
            if cached is not None:
                result[cuenta_id] = cached
            else:
                missing.append(cuenta_id)
        if not missing or not self._settings.token or self._clock() < self._down_until:
            return result
        for start in range(0, len(missing), 100):
            chunk = missing[start:start + 100]
            try:
                response = self._client.post(f"/internal/v1/entitlements/{tipo}/batch", headers=self._headers(),
                                             json={"ids": [str(i) for i in chunk]})
                if response.status_code != 200:
                    logger.warning("ms-pago answered %s for a batch of plans: not marking featured accounts",
                                   response.status_code)
                    continue
                for item in response.json()["items"]:
                    cuenta_id = UUID(item["id"])
                    self._store((tipo, cuenta_id), item)
                    result[cuenta_id] = item
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
                if isinstance(error, httpx.TransportError):
                    self._down_until = self._clock() + DOWN_BACKOFF_SECONDS
                logger.warning("ms-pago is not available for a batch of plans: %s", error)
                break
        return result


def es_pro(entitlement: dict[str, Any] | None) -> bool:
    return bool(entitlement) and entitlement.get("plan") == "PRO"


def es_destacado(entitlement: dict[str, Any] | None) -> bool:
    return bool(entitlement) and "destacado" in (entitlement.get("beneficios") or [])


def limite(entitlement: dict[str, Any] | None, recurso: str) -> tuple[int | None, int | None]:
    """(maximo, ventana_horas) del recurso; (None, None) si no hay límite o no se sabe."""
    if not entitlement:
        return None, None
    regla = (entitlement.get("limites") or {}).get(recurso)
    if not isinstance(regla, dict):
        return None, None
    maximo, ventana = regla.get("maximo"), regla.get("ventana_horas")
    return (maximo if isinstance(maximo, int) else None), (ventana if isinstance(ventana, int) else None)


# counter(desde): con ventana, las fechas de creación desde `desde`; sin ventana (desde=None), cuántas hay ahora.
Counter = Callable[[datetime | None], "int | list[datetime]"]


def enforce(client: EntitlementsClient | None, tipo: str, cuenta_id: UUID, recurso: str, counter: Counter,
            now: datetime | None = None) -> None:
    """Lanza LimitePlanError si la cuenta ya usó todo su plan para `recurso`.

    Sin cliente, sin respuesta de ms-pago o sin límite: no hace nada (fail-open / Pro)."""
    if client is None:
        return
    maximo, ventana = limite(client.get(tipo, cuenta_id), recurso)
    if maximo is None:
        return
    now = now or datetime.now(timezone.utc)
    desde = now - timedelta(hours=ventana) if ventana else None
    contado = counter(desde)
    if isinstance(contado, int):
        usado, libera = contado, None
    else:
        fechas = sorted(contado)
        usado = len(fechas)
        # Se libera un cupo cuando la que hace sobrar el límite sale de la ventana.
        libera = fechas[usado - maximo] + timedelta(hours=ventana) if ventana and usado >= maximo else None
    if usado >= maximo:
        raise LimitePlanError(recurso, maximo, usado, libera, ventana)
