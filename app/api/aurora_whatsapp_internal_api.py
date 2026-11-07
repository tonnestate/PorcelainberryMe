"""Internal n8n-only Aurora reply endpoint for the WhatsApp bridge."""
import os
from flask import Blueprint, current_app, g, request
from sqlalchemy import or_

from app.api.workflow_common import (error_response, require_n8n_auth, require_owner_mcp_send_auth, success_response)
from app.database.connection import get_db
from app.models.user import User
from app.services.aurora_whatsapp_orchestrator import AuroraWhatsAppOrchestrator
from app.services.aurora_whatsapp_send_service import send_via_bridge
from app.services.porcelainberry_whatsapp_mirror import mirror_owner_inbound
from app.services.aurora_whatsapp_setting import is_enabled
from app.services.aurora_weather_alert_runner import run as run_weather_alerts
from app.models.aurora_news import AuroraNewsItem
from app.services.aurora_news_pipeline import fingerprint as news_fingerprint, is_candidate
from app.services.aurora_news_triage import parse_triage
from app.services.aurora_news_triage_runner import run as run_news_triage
from sqlalchemy.exc import IntegrityError

bp_aurora_whatsapp_internal = Blueprint(
    "aurora_whatsapp_internal", __name__, url_prefix="/api/internal/v1/aurora"
)

_orchestrator = None


def _owner_scope():
    email = os.getenv("TE_AURORA_OWNER_EMAIL", "redacted@example.invalid").strip().lower()
    username = os.getenv("TE_AURORA_OWNER_USERNAME", "bjorn.tonn").strip()
    owner = get_db().query(User).filter(
        User.is_enabled == 1,
        or_(User.email == email, User.username == username, User.wp_username == email),
    ).first()
    if owner is None:
        raise LookupError("AURORA_OWNER_UNAVAILABLE")
    return owner.tenant_id, owner.user_id, "personal"


def _gateway():
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = AuroraWhatsAppOrchestrator()
    return _orchestrator


@bp_aurora_whatsapp_internal.post("/reply")
@require_n8n_auth(require_tenant=False)
def reply():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error_response(
            "VALIDATION_ERROR", "WhatsApp event must be a JSON object.",
            status=400,
        )
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip() or len(message) > 6000:
        return error_response("VALIDATION_ERROR", "message is invalid.", {"field": "message"})
    try:
        tenant_id, owner_id, domain_id = _owner_scope()
        # The owner MCP inbox is independently opt-in. An accepted inbound
        # WhatsApp event must be durably queued before acknowledging n8n;
        # otherwise a full/damaged disk can silently lose a personal message.
        # No provider/network call is made in this request.
        try:
            mirror_owner_inbound(
                {**payload, "message": message.strip()},
                tenant_id=tenant_id, owner_id=owner_id,
            )
        except Exception:
            # No contact, payload, file path or upstream exception in logs.
            current_app.logger.error("porcelainberry_whatsapp_mirror_persist_failed")
            return error_response(
                "WHATSAPP_MIRROR_PERSIST_FAILED",
                "Authenticated WhatsApp event could not be durably stored.",
                status=503,
            )
        if not is_enabled(get_db(), tenant_id, owner_id):
            return success_response({"send": False, "text": "", "decision": "MASTER_OFF"})
        result = _gateway().handle(
            {**payload, "message": message.strip()}, tenant_id=tenant_id,
            owner_id=owner_id, domain_id=domain_id,
        )
    except Exception:
        current_app.logger.exception("aurora_whatsapp_orchestrator_failed")
        return error_response("AURORA_UNAVAILABLE", "Aurora ist momentan nicht erreichbar.", status=503)
    return success_response(result)


@bp_aurora_whatsapp_internal.post("/send")
@require_owner_mcp_send_auth
def send():
    """Owner-only outbound delivery initiated by an approved automation."""
    payload = request.get_json(silent=True) or {}
    try:
        _owner_scope()  # Fail closed if Aurora's owner is unavailable.
        result = send_via_bridge({**payload, "idempotency_key": g.idempotency_key})
    except ValueError as error:
        return error_response("VALIDATION_ERROR", "Aurora message could not be sent.",
                              {"reason": str(error)}, status=400)
    except LookupError:
        return error_response("AURORA_UNAVAILABLE", "Aurora owner is unavailable.", status=503)
    return success_response(result)


@bp_aurora_whatsapp_internal.post("/weather-alerts/check")
@require_n8n_auth(require_tenant=False)
def weather_alert_check():
    try:
        return success_response(run_weather_alerts(get_db()))
    except Exception:
        current_app.logger.exception("aurora_weather_alert_check_failed")
        return error_response("WEATHER_ALERT_UNAVAILABLE", "Weather alerts are unavailable.", status=503)


@bp_aurora_whatsapp_internal.post("/news/ingest")
@require_n8n_auth(require_tenant=False)
def news_ingest():
    """Persist only new, local source items; triage and delivery happen downstream."""
    payload = request.get_json(silent=True) or {}
    items = payload.get("items")
    if not isinstance(items, list) or len(items) > 50:
        return error_response("VALIDATION_ERROR", "items must contain at most 50 entries.", status=400)
    tenant_id, owner_id, _ = _owner_scope()
    db, inserted, skipped = get_db(), 0, 0
    for item in items:
        if not isinstance(item, dict):
            skipped += 1; continue
        source = str(item.get("source") or "").upper()
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        content = str(item.get("content") or "").strip()
        if not source or not title or not url or len(title) > 1000 or len(url) > 1024 or not is_candidate(source, f"{title} {content}"):
            skipped += 1; continue
        record = AuroraNewsItem(tenant_id=tenant_id, owner_user_id=owner_id,
            fingerprint=news_fingerprint({**item, "source": source}), source=source,
            source_url=url, title=title, content=content[:12000], status="PENDING")
        try:
            db.add(record); db.commit(); inserted += 1
        except IntegrityError:
            db.rollback(); skipped += 1
    return success_response({"inserted": inserted, "skipped": skipped})


@bp_aurora_whatsapp_internal.post("/news/triage")
@require_n8n_auth(require_tenant=False)
def news_triage():
    payload = request.get_json(silent=True) or {}
    item_id = str(payload.get("item_id") or "").strip()
    decision = parse_triage(payload.get("decision"))
    if not item_id or decision is None:
        return error_response("VALIDATION_ERROR", "item_id or decision is invalid.", status=400)
    tenant_id, owner_id, _ = _owner_scope()
    db = get_db()
    record = db.query(AuroraNewsItem).filter(
        AuroraNewsItem.aurora_news_item_id == item_id,
        AuroraNewsItem.tenant_id == tenant_id,
        AuroraNewsItem.owner_user_id == owner_id,
        AuroraNewsItem.status == "PENDING",
    ).first()
    if record is None:
        return success_response({"updated": False})
    record.status = "READY" if decision["relevant"] else "FILTERED"
    record.category, record.priority = decision["category"], decision["priority"]
    record.location, record.briefing = decision["location"], decision["briefing"]
    from utils.timestamp import now_ts
    record.triaged_at = now_ts()
    db.commit()
    return success_response({"updated": True, "status": record.status})


@bp_aurora_whatsapp_internal.post("/news/triage/run")
@require_n8n_auth(require_tenant=False)
def news_triage_run():
    try:
        return success_response(run_news_triage(get_db()))
    except Exception:
        current_app.logger.exception("aurora_news_triage_failed")
        return error_response("AURORA_NEWS_UNAVAILABLE", "News triage is unavailable.", status=503)
