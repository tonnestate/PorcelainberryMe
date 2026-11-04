"""Human-authenticated self-call approval and dispatch over existing control.

No public dialer, no arbitrary target from a model or request body. A
verified, enrolled human explicitly requests the *configured local AI*
to place a WebRTC call to that human identity. All real ringing checks
still run through CallRegistry.originate in the existing voice worker.
"""
from __future__ import annotations

from contextlib import closing

from .ip_calls import CallError, IDEMPOTENCY, _identity
from .outbound_jobs import CallApprovalError, DurableCallJobs


class AuthenticatedSelfCall:
    def __init__(self, registry, jobs: DurableCallJobs, *, agent_id: str):
        if not isinstance(jobs, DurableCallJobs):
            raise ValueError("trusted_outbound_job_store_required")
        self.agent_id = _identity(agent_id)
        self.registry = registry
        self.jobs = jobs

    def _human(self, claims):
        if (not isinstance(claims, dict)
                or claims.get("role") != "human"):
            raise CallError("only_authenticated_human_can_request_call", 403)
        tenant = _identity(claims.get("tenant"))
        recipient = _identity(claims.get("sub"))
        if recipient == self.agent_id:
            raise CallError("self_call_agent_identity_invalid", 403)
        return tenant, recipient

    def request(self, claims, body: dict) -> dict:
        tenant, recipient = self._human(claims)
        if (not isinstance(body, dict) or set(body) != {"idempotency_key"}
                or not isinstance(body["idempotency_key"], str)
                or not IDEMPOTENCY.fullmatch(body["idempotency_key"])):
            raise CallError("stable_self_call_request_id_required")
        # No source, destination, agent or role is taken from the payload.
        with closing(self.registry._conn()) as db:
            human = db.execute("""
                SELECT agent_calls,do_not_disturb FROM ip_call_users
                WHERE tenant=? AND subject=?
            """, (tenant, recipient)).fetchone()
            agent = db.execute("""
                SELECT 1 FROM ip_call_users
                 WHERE tenant=? AND subject=?
            """, (tenant, self.agent_id)).fetchone()
            if human is None or agent is None:
                raise CallError("account_not_enrolled", 403)
            if not human["agent_calls"]:
                raise CallError("agent_call_not_opted_in", 403)
            if human["do_not_disturb"]:
                raise CallError("recipient_do_not_disturb", 409)
        # Stable click event ID survives network loss and app reload. The
        # service never accepts arbitrary consent evidence from the browser.
        evidence = (
            "authenticated-call-me-v1:" + tenant + ":" + recipient + ":"
            + body["idempotency_key"]
        )
        try:
            approval_id = self.jobs.approve(
                tenant=tenant, agent=self.agent_id, recipient=recipient,
                consent_reference=evidence,
            )
            return self.jobs.request(
                approval_id=approval_id, tenant=tenant,
                agent=self.agent_id, recipient=recipient,
            )
        except CallApprovalError as exc:
            if "capacity" in str(exc):
                raise CallError("call_me_queue_capacity_exceeded", 429) from exc
            raise CallError("call_me_approval_rejected", 409) from exc

    def status(self, claims, approval_id: str) -> dict:
        """Read only this signed human's fixed-recipient approval state."""
        tenant, recipient = self._human(claims)
        try:
            return self.jobs.status(
                approval_id=approval_id, tenant=tenant,
                agent=self.agent_id, recipient=recipient,
            )
        except (CallApprovalError, PermissionError, TypeError) as exc:
            raise CallError("call_me_approval_not_found", 404) from exc

    def revoke(self, claims, body: dict) -> dict:
        tenant, recipient = self._human(claims)
        if not isinstance(body, dict) or set(body) != {"approval_id"}:
            raise CallError("self_call_revocation_id_required")
        try:
            return self.jobs.revoke(
                approval_id=body["approval_id"], tenant=tenant,
                agent=self.agent_id, recipient=recipient,
            )
        except (CallApprovalError, PermissionError, TypeError) as exc:
            # Do not reveal whether another tenant's approval exists.
            raise CallError("call_me_approval_not_found", 404) from exc
