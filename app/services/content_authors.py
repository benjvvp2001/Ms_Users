"""HU-47: quién hizo el contenido que se reportó (una actividad o un mensaje del chat).

Esos datos no viven en Ms_Users: la actividad es de Ms_Activities y el mensaje, de Ms_Matching. Aquí se les pregunta por
la red interna, con el secreto compartido INTERNAL_SERVICE_TOKEN en X-Internal-Service-Token (esas rutas nunca están en
el gateway). Si el servicio de origen no responde, NO se inventa nada: el resultado es "unavailable" y el reporte se
devuelve sin autor, marcado como tal."""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

logger = logging.getLogger(__name__)

MIN_TOKEN_BYTES = 32
Status = Literal["found", "missing", "unavailable"]
_MESSAGE_TARGET = re.compile(r"^([0-9a-fA-F-]{36}):([0-9]{1,18})$")
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass(frozen=True)
class AuthorResult:
    """found: the account. missing: the content does not exist (or has no author). unavailable: the origin service
    did not answer or refused, so nothing is known."""

    status: Status
    user_id: UUID | None = None


FOUND_NOTHING = AuthorResult("missing")
UNAVAILABLE = AuthorResult("unavailable")


class ContentAuthors(Protocol):
    def lookup(self, tipo: str, objetivo_id: str) -> AuthorResult: ...


class InternalContentAuthors:
    """GET /internal/v1/activities/{id}/author (Ms_Activities) and
    GET /internal/v1/messages/{match}/{message}/author (Ms_Matching). Only the standard library: no new dependency."""

    def __init__(self, activities_url: str | None, matching_url: str | None, token: str | None,
                 timeout: float = 2.0) -> None:
        self._activities = (activities_url or "").rstrip("/") or None
        self._matching = (matching_url or "").rstrip("/") or None
        self._token = token if token and len(token.encode("utf-8")) >= MIN_TOKEN_BYTES else None
        self._timeout = timeout

    def lookup(self, tipo: str, objetivo_id: str) -> AuthorResult:
        if tipo == "actividad":
            try:
                activity_id = UUID(objetivo_id)
            except ValueError:
                return FOUND_NOTHING
            return self._get(self._activities, f"/internal/v1/activities/{activity_id}/author")
        if tipo == "mensaje":
            parts = _MESSAGE_TARGET.match(objetivo_id)
            if parts is None:
                return FOUND_NOTHING
            try:
                match_id = UUID(parts.group(1))
            except ValueError:
                return FOUND_NOTHING
            return self._get(self._matching, f"/internal/v1/messages/{match_id}/{int(parts.group(2))}/author")
        return FOUND_NOTHING

    def _get(self, base: str | None, path: str) -> AuthorResult:
        if base is None or self._token is None:
            logger.error("Content author lookup is not configured (service URL or INTERNAL_SERVICE_TOKEN)")
            return UNAVAILABLE
        request = urllib.request.Request(base + path, headers={"X-Internal-Service-Token": self._token}, method="GET")
        try:
            with _OPENER.open(request, timeout=self._timeout) as response:   # internal http URL, never through a proxy
                body = response.read(4096)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return FOUND_NOTHING
            logger.error("The origin service refused the author lookup: HTTP %s", error.code)
            return UNAVAILABLE
        except (urllib.error.URLError, OSError, ValueError) as error:
            logger.error("The origin service did not answer the author lookup: %s", type(error).__name__)
            return UNAVAILABLE
        try:
            return AuthorResult("found", UUID(str(json.loads(body)["user_id"])))
        except (ValueError, KeyError, TypeError):
            logger.error("The origin service sent an author that cannot be read")
            return UNAVAILABLE
