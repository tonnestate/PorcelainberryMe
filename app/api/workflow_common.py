"""
Gemeinsame Bausteine fuer die Workflow-Engine-Endpunkte (w1.txt): einheitliches
Antwortformat, Bearer-/Tenant-Pruefung fuer den technischen n8n-Client,
Fehlercode-Konstanten.

Auth-Design-Entscheidung: w1.txt Abschnitt 4 verlangt Pruefung von "Bearer-
Token gueltig", "technischer Client ist aktiviert" und "technischer Client
darf auf den angegebenen Tenant zugreifen", nennt dafuer aber einen EINZELNEN
Token TE_FLASK_API_TOKEN (kein Tenant-Bezug im Tokenwert) und listet keine
neue Tabelle dafuer (Abschnitt 5 zaehlt die zu ergaenzenden Modelle
abschliessend auf, ein Client-Registry-Modell ist nicht darunter; Abschnitt 23
verbietet zudem neue fachliche Tabellen). Es gibt in TonnEstate genau eine
n8n-Instanz fuer alle Mandanten (kein Mandant hat eine eigene n8n-Instanz) -
deshalb: EIN globales Secret authentifiziert "den technischen Client n8n" an
sich, die Tenant-Zugriffspruefung reduziert sich darauf, dass der angegebene
Tenant real existiert (bereits an anderer Stelle noetig) statt einer
Client-zu-Tenant-ACL. Dokumentiert als bewusste Vereinfachung, falls spaeter
mehrere technische Clients mit unterschiedlichen Tenant-Rechten noetig werden,
braucht das dann eine eigene Tabelle.
"""

import os
import uuid
import hmac
import json
import stat
from functools import wraps
from flask import request, jsonify, g
from app.database.connection import get_db
from app.models.tenant import Tenant


MAX_TENANT_ID_LENGTH = 36
MAX_IDEMPOTENCY_KEY_LENGTH = 255
MAX_CORRELATION_ID_LENGTH = 255

ERROR_STATUS = {
    "AUTHENTICATION_REQUIRED": 401,
    "AUTHENTICATION_INVALID": 401,
    "TENANT_SCOPE_VIOLATION": 403,
    "WORKFLOW_DEFINITION_NOT_FOUND": 404,
    "WORKFLOW_NOT_ACTIVE": 409,
    "WORKFLOW_NOT_MATERIALIZED": 409,
    "WORKFLOW_INSTANCE_NOT_FOUND": 404,
    "WORKFLOW_INSTANCE_ALREADY_ACTIVE": 409,
    "WORKFLOW_INSTANCE_RESPONSE_INVALID": 500,
    "WORKFLOW_TRANSITION_NOT_FOUND": 404,
    "WORKFLOW_TRANSITION_NOT_ALLOWED": 422,
    "WORKFLOW_MANUAL_APPROVAL_REQUIRED": 422,
    "WORKFLOW_DECISION_REQUIRED": 422,
    "WORKFLOW_DECISION_RESULT_MISMATCH": 422,
    "WORKFLOW_LOCK_CONFLICT": 409,
    "DECISION_MODEL_NOT_FOUND": 404,
    "DECISION_MODEL_NOT_ACTIVE": 409,
    "DECISION_INPUT_INVALID": 422,
    "DECISION_RULE_INVALID": 422,
    "DECISION_EXECUTION_FAILED": 500,
    "IDEMPOTENCY_KEY_REQUIRED": 400,
    "IDEMPOTENCY_KEY_REUSED": 409,
    "CASE_NOT_FOUND": 404,
    "CONTACT_NOT_FOUND": 404,
    "PROPERTY_NOT_FOUND": 404,
    "OBJECT_TENANT_MISMATCH": 403,
    "VALIDATION_ERROR": 400,
}


class WorkflowApiError(Exception):
    def __init__(self, error_code, message, details=None):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.details = details or {}


def success_response(data, status=200, idempotent_replay=False):
    return jsonify({
        "success": True,
        "data": data,
        "meta": {
            "correlation_id": getattr(g, "correlation_id", None),
            "idempotent_replay": idempotent_replay,
        },
    }), status


def error_response(error_code, message, details=None, status=None):
    if status is None:
        status = ERROR_STATUS.get(error_code, 400)
    return jsonify({
        "success": False,
        "error_code": error_code,
        "message": message,
        "details": details or {},
        "meta": {
            "correlation_id": getattr(g, "correlation_id", None),
        },
    }), status


def _valid_identifier(value, *, field, maximum):
    """Return a bounded identifier or ``None`` with a stable validation error.

    Workflow identifiers are persisted in bounded MariaDB columns.  Rejecting
    malformed values at the edge avoids type errors, accidental trimming, and
    database-originated 500 responses.
    """
    if not isinstance(value, str):
        return None, error_response(
            "VALIDATION_ERROR", f"{field} must be a string.", {"field": field},
        )
    if not value or value != value.strip() or len(value) > maximum:
        return None, error_response(
            "VALIDATION_ERROR", f"{field} is invalid.", {"field": field},
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None, error_response(
            "VALIDATION_ERROR", f"{field} is invalid.", {"field": field},
        )
    return value, None


def require_n8n_auth(require_idempotency_key=False, require_tenant=True):
    """
    Decorator fuer alle Workflow-Engine-Routen. Prueft Bearer-Token,
    liest/erzeugt Correlation-ID, prueft Tenant-Konsistenz zwischen
    X-Tenant-ID-Header, URL-Pfad (kwargs["tenant_id"]) und JSON-Body
    (body["tenant_id"]) - alle drei muessen, soweit vorhanden,
    uebereinstimmen. Speichert korrelierte Werte auf flask.g fuer die
    Route-Funktion.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            raw_correlation_id = request.headers.get("X-Correlation-ID")
            if raw_correlation_id is None:
                g.correlation_id = str(uuid.uuid4())
            else:
                correlation_id, correlation_error = _valid_identifier(
                    raw_correlation_id, field="correlation_id",
                    maximum=MAX_CORRELATION_ID_LENGTH,
                )
                if correlation_error:
                    g.correlation_id = str(uuid.uuid4())
                    return correlation_error
                g.correlation_id = correlation_id

            expected_token = os.getenv("TE_FLASK_API_TOKEN")
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return error_response("AUTHENTICATION_REQUIRED", "Missing bearer token.")
            provided_token = auth_header[len("Bearer "):].strip()

            if not expected_token or not hmac.compare_digest(provided_token, expected_token):
                return error_response("AUTHENTICATION_INVALID", "Invalid bearer token.")

            body = request.get_json(silent=True)
            if body is None:
                body = {}
            if not isinstance(body, dict):
                return error_response(
                    "VALIDATION_ERROR", "JSON body must be an object.", {"field": "body"},
                )
            header_tenant = request.headers.get("X-Tenant-ID")
            path_tenant = kwargs.get("tenant_id")
            body_tenant = body.get("tenant_id")

            candidates = []
            for raw_tenant in (header_tenant, path_tenant, body_tenant):
                if raw_tenant is None:
                    continue
                tenant_value, tenant_error = _valid_identifier(
                    raw_tenant, field="tenant_id", maximum=MAX_TENANT_ID_LENGTH,
                )
                if tenant_error:
                    return tenant_error
                candidates.append(tenant_value)
            if candidates and len(set(candidates)) > 1:
                return error_response(
                    "TENANT_SCOPE_VIOLATION", "Tenant scope mismatch.",
                    {"header_tenant_id": header_tenant, "path_tenant_id": path_tenant, "body_tenant_id": body_tenant},
                )

            tenant_id = candidates[0] if candidates else None
            if require_tenant:
                if not tenant_id:
                    return error_response("VALIDATION_ERROR", "tenant_id is required (header, path or body).")
                db = get_db()
                if not db.query(Tenant).filter(Tenant.id == tenant_id).first():
                    return error_response("TENANT_SCOPE_VIOLATION", "Unknown tenant.", {"tenant_id": tenant_id})
                g.tenant_id = tenant_id
            else:
                # Global technical endpoints are exceptional.  They must not
                # silently reinterpret a tenant-bound request as global.
                if tenant_id:
                    return error_response("VALIDATION_ERROR", "tenant_id is not permitted for this endpoint.",
                                          {"field": "tenant_id"})
                g.tenant_id = None

            if request.method in ("POST", "PUT") and require_idempotency_key:
                raw_idem_key = request.headers.get("X-Idempotency-Key")
                if raw_idem_key is None:
                    return error_response("IDEMPOTENCY_KEY_REQUIRED", "X-Idempotency-Key header is required for this operation.")
                idem_key, idempotency_error = _valid_identifier(
                    raw_idem_key, field="idempotency_key",
                    maximum=MAX_IDEMPOTENCY_KEY_LENGTH,
                )
                if idempotency_error:
                    return idempotency_error
                g.idempotency_key = idem_key
            else:
                raw_idem_key = request.headers.get("X-Idempotency-Key")
                if raw_idem_key is None:
                    g.idempotency_key = None
                else:
                    idem_key, idempotency_error = _valid_identifier(
                        raw_idem_key, field="idempotency_key",
                        maximum=MAX_IDEMPOTENCY_KEY_LENGTH,
                    )
                    if idempotency_error:
                        return idempotency_error
                    g.idempotency_key = idem_key

            return fn(*args, **kwargs)
        return wrapper
    return decorator




def _private_owner_mcp_send_token():
    """Read a dedicated owner-send credential without following links.

    This is never the global n8n bearer; its scope is enforced by applying
    the wrapper only to /api/internal/v1/aurora/send.
    """
    filename = os.getenv("TE_PB_MCP_SEND_TOKEN_FILE", "")
    if not filename or not os.path.isabs(filename):
        return None
    try:
        fd = os.open(filename, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode)
                    or stat.S_IMODE(info.st_mode) & 0o077
                    or info.st_uid != os.geteuid()
                    or info.st_nlink != 1):
                return None
            token = stream.read(4097).strip()
    except (OSError, UnicodeError, ValueError):
        return None
    if (not 32 <= len(token) <= 4096
            or any(char.isspace() for char in token)
            or hmac.compare_digest(token, os.getenv("TE_FLASK_API_TOKEN", ""))):
        return None
    return token


def require_owner_mcp_send_auth(fn):
    """Allow a dedicated owner-MCP token *only* on the existing send route.

    Existing n8n callers keep their original bearer and validation behavior.
    The dedicated bearer never authenticates /reply, workflow, or other API.
    """
    n8n_only = require_n8n_auth(
        require_idempotency_key=True, require_tenant=False,
    )(fn)

    @wraps(fn)
    def wrapper(*args, **kwargs):
        provided = request.headers.get("Authorization", "")
        dedicated = _private_owner_mcp_send_token()
        if not dedicated or not provided.startswith("Bearer ") or not hmac.compare_digest(
                provided[len("Bearer "):].strip(), dedicated):
            return n8n_only(*args, **kwargs)

        raw_correlation = request.headers.get("X-Correlation-ID")
        if raw_correlation is None:
            g.correlation_id = str(uuid.uuid4())
        else:
            correlation, failure = _valid_identifier(
                raw_correlation, field="correlation_id",
                maximum=MAX_CORRELATION_ID_LENGTH,
            )
            if failure:
                g.correlation_id = str(uuid.uuid4())
                return failure
            g.correlation_id = correlation

        body = request.get_json(silent=True)
        if body is None:
            body = {}
        if not isinstance(body, dict):
            return error_response("VALIDATION_ERROR", "JSON body must be an object.",
                                  {"field": "body"})
        if (request.headers.get("X-Tenant-ID") is not None
                or "tenant_id" in body
                or kwargs.get("tenant_id") is not None):
            return error_response("VALIDATION_ERROR",
                                  "tenant_id is not permitted for this endpoint.",
                                  {"field": "tenant_id"})
        g.tenant_id = None

        key = request.headers.get("X-Idempotency-Key")
        if key is None:
            return error_response("IDEMPOTENCY_KEY_REQUIRED",
                                  "X-Idempotency-Key header is required for this operation.")
        idem, failure = _valid_identifier(
            key, field="idempotency_key",
            maximum=MAX_IDEMPOTENCY_KEY_LENGTH,
        )
        if failure:
            return failure
        g.idempotency_key = idem
        return fn(*args, **kwargs)

    return wrapper


def parse_json_field(raw_text, default=None):
    """Kontrolliertes Deserialisieren von LONGTEXT-JSON-Spalten. Ungueltiges
    JSON wird NIE stillschweigend ignoriert, sondern als WorkflowApiError
    gemeldet (w1.txt Abschnitt 5: "ungueltiges JSON kontrolliert als Fehler
    melden")."""
    if raw_text is None or raw_text == "":
        return default
    try:
        return json.loads(raw_text)
    except (TypeError, ValueError) as exc:
        raise WorkflowApiError(
            "DECISION_RULE_INVALID" if default is None else "VALIDATION_ERROR",
            "Stored JSON payload is not valid JSON.",
            {"error": str(exc)},
        )
