"""Verify dedicated owner MCP bearer cannot authorize the n8n API broadly.

Extract the actual production decorators with AST so this standalone module
can test Flask authorization semantics without importing the full CRM runtime.
No network/provider calls and no real credentials.
"""
from __future__ import annotations

import ast
from functools import wraps
import hmac
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "app" / "api" / "workflow_common.py"
API = Path(__file__).resolve().parents[1] / "app" / "api" / "aurora_whatsapp_internal_api.py"


class OwnerSendAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.token_file = Path(self.temp.name) / "owner-mcp-token"
        self.token = "m" * 64
        self.legacy = "n" * 64
        self.token_file.write_text(self.token, encoding="utf-8")
        self.token_file.chmod(0o600)
        self.env = patch.dict(os.environ, {
            "TE_PB_MCP_SEND_TOKEN_FILE": str(self.token_file),
            "TE_FLASK_API_TOKEN": self.legacy,
        })
        self.env.start()
        self.globals = self._load_real_decorators()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _load_real_decorators(self):
        source = ast.parse(SOURCE.read_text(encoding="utf-8"))
        methods = {"_valid_identifier", "require_n8n_auth",
                   "_private_owner_mcp_send_token", "require_owner_mcp_send_auth"}
        definitions = [node for node in source.body
                       if isinstance(node, ast.FunctionDef) and node.name in methods]
        if len(definitions) != 4:
            raise RuntimeError("missing_real_auth_functions")
        module = ast.fix_missing_locations(ast.Module(body=definitions, type_ignores=[]))
        scope = {
            "os": os, "stat": stat, "hmac": hmac, "uuid": uuid, "wraps": wraps,
            "MAX_TENANT_ID_LENGTH": 36, "MAX_IDEMPOTENCY_KEY_LENGTH": 255,
            "MAX_CORRELATION_ID_LENGTH": 255,
            "g": SimpleNamespace(),
            "error_response": lambda code, message, details=None, status=None: (
                "ERROR", code, status or 400,
            ),
        }
        exec(compile(module, str(SOURCE), "exec"), scope)
        return scope

    def _call(self, bearer, *, payload=None, extra=None, dedicated=True):
        headers = {
            "Authorization": "Bearer " + bearer,
            "X-Idempotency-Key": "fixed-event-id-001",
        }
        headers.update(extra or {})
        ctx = self.globals
        ctx["request"] = SimpleNamespace(
            headers=headers, get_json=lambda silent=True: {} if payload is None else payload,
            method="POST",
        )
        calls = []
        def endpoint():
            calls.append(ctx["g"].idempotency_key)
            return "ALLOWED"
        wrapped = (ctx["require_owner_mcp_send_auth"](endpoint) if dedicated
                   else ctx["require_n8n_auth"](
                       require_idempotency_key=True, require_tenant=False,
                   )(endpoint))
        return wrapped(), calls

    def test_private_mcp_token_authorizes_only_the_send_wrapper(self):
        result, calls = self._call(self.token)
        self.assertEqual(result, "ALLOWED")
        self.assertEqual(calls, ["fixed-event-id-001"])
        blocked, calls = self._call(self.token, dedicated=False)
        self.assertEqual(blocked[:2], ("ERROR", "AUTHENTICATION_INVALID"))
        self.assertEqual(calls, [])

    def test_original_n8n_bearer_preserves_existing_send_route(self):
        result, calls = self._call(self.legacy)
        self.assertEqual(result, "ALLOWED")
        self.assertEqual(calls, ["fixed-event-id-001"])

    def test_private_token_rejects_tenant_override_and_missing_idempotency(self):
        for kwargs in (
            {"payload": {"tenant_id": "foreign"}},
            {"extra": {"X-Tenant-ID": "foreign"}},
            {"extra": {"X-Idempotency-Key": ""}},
        ):
            with self.subTest(kwargs=kwargs):
                result, calls = self._call(self.token, **kwargs)
                self.assertEqual(result[0], "ERROR")
                self.assertEqual(calls, [])

    def test_missing_or_public_or_hardlinked_token_fails_closed(self):
        self.token_file.chmod(0o644)
        self.assertEqual(self._call(self.token)[0][:2],
                         ("ERROR", "AUTHENTICATION_INVALID"))
        self.token_file.chmod(0o600)
        alias = Path(self.temp.name) / "alias"
        os.link(self.token_file, alias)
        try:
            self.assertEqual(self._call(self.token)[0][:2],
                             ("ERROR", "AUTHENTICATION_INVALID"))
        finally:
            alias.unlink()
        self.token_file.unlink()
        self.assertEqual(self._call(self.token)[0][:2],
                         ("ERROR", "AUTHENTICATION_INVALID"))

    def test_untrusted_token_never_falls_through_to_send(self):
        result, calls = self._call("x" * 64)
        self.assertEqual(result[:2], ("ERROR", "AUTHENTICATION_INVALID"))
        self.assertEqual(calls, [])

    def test_send_route_uses_real_dedicated_decorator(self):
        source = ast.parse(API.read_text(encoding="utf-8"))
        fn = next(node for node in source.body
                  if isinstance(node, ast.FunctionDef) and node.name == "send")
        names = [item.id for item in fn.decorator_list if isinstance(item, ast.Name)]
        self.assertEqual(names, ["require_owner_mcp_send_auth"])


if __name__ == "__main__":
    unittest.main()
