"""Self-hosted LiveKit Twirp room teardown; no cloud service or SIP needed.

DeleteRoom forcibly disconnects room participants. The caller must keep a
durable reconciliation outbox because HTTP acknowledgement and SQLite state
cannot form one atomic transaction.
"""
from __future__ import annotations

import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .ip_calls import _jwt


class MediaTeardownError(RuntimeError):
    pass


class LiveKitRoomTerminator:
    """Minimal stdlib implementation of the documented RoomService/DeleteRoom."""

    def __init__(self, media_url: str, api_key: str, api_secret: str,
                 *, timeout_seconds: float = 3.0, http_open=urlopen, clock=time.time):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("livekit_admin_key_required")
        if not isinstance(api_secret, str) or len(api_secret) < 32:
            raise ValueError("livekit_admin_secret_required")
        parsed = urlsplit(media_url)
        if (parsed.scheme not in ("ws", "wss") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query
                or parsed.fragment or parsed.path not in ("", "/")):
            raise ValueError("invalid_livekit_server_url")
        if not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 10:
            raise ValueError("invalid_media_timeout")
        self.endpoint = urlunsplit((
            "http" if parsed.scheme == "ws" else "https",
            parsed.netloc, "/twirp/livekit.RoomService/DeleteRoom", "", "",
        ))
        self.remove_endpoint = self.endpoint.replace(
            "/DeleteRoom", "/RemoveParticipant",
        )
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout = timeout_seconds
        self.http_open = http_open
        self.clock = clock

    def delete_room(self, room: str) -> None:
        if not isinstance(room, str) or not room.startswith("pb_") or len(room) != 35:
            raise ValueError("invalid_managed_room")
        try:
            int(room[3:], 16)
        except ValueError as exc:
            raise ValueError("invalid_managed_room") from exc
        now = int(self.clock())
        # LiveKit DeleteRoom requires roomCreate permission; no room-wide
        # participant, recording, ingress or SIP privilege is granted.
        authorization = _jwt({
            "iss": self.api_key,
            "iat": now, "nbf": now - 1, "exp": now + 30,
            "video": {"roomCreate": True},
        }, self.api_secret)
        request = Request(
            self.endpoint, method="POST",
            data=json.dumps({"room": room}, separators=(",", ":")).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + authorization,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with self.http_open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise MediaTeardownError("livekit_delete_not_confirmed")
                # Success is confirmed by Twirp HTTP 200; do not require a
                # response body since DeleteRoomResponse can be empty.
                response.read(1024)
        except HTTPError as exc:
            # An absent room is already empty. Only a typed Twirp not_found
            # response is accepted; not a generic proxy/route 404.
            if exc.code == 404:
                try:
                    payload = json.loads(exc.read(2048))
                    if payload.get("code") == "not_found":
                        return
                except (ValueError, AttributeError, TypeError):
                    pass
            raise MediaTeardownError("livekit_delete_http_failed") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise MediaTeardownError("livekit_delete_unreachable") from exc

    def remove_participant(self, room: str, identity: str) -> None:
        """Remove only a revoked staff participant, NEVER their customer's room.

        The self-hosted server does not revoke existing join JWTs. The
        durable control-plane outbox must repeat this operation through
        token expiry plus clock skew to defeat immediate reconnection.
        """
        if not isinstance(room, str) or len(room) != 35 or not room.startswith("pb_"):
            raise ValueError("invalid_managed_room")
        if not isinstance(identity, str) or len(identity) != 27 or not identity.startswith("ip_"):
            raise ValueError("invalid_managed_participant")
        try:
            int(room[3:], 16)
            int(identity[3:], 16)
        except ValueError as exc:
            raise ValueError("invalid_managed_participant") from exc
        now = int(self.clock())
        token = _jwt({
            "iss": self.api_key,
            "iat": now, "nbf": now - 1, "exp": now + 30,
            "video": {"roomAdmin": True, "room": room},
        }, self.api_secret)
        req = Request(
            self.remove_endpoint, method="POST",
            data=json.dumps(
                {"room": room, "identity": identity},
                separators=(",", ":"),
            ).encode(),
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with self.http_open(req, timeout=self.timeout) as response:
                if response.status != 200:
                    raise MediaTeardownError("livekit_staff_remove_not_confirmed")
                response.read(1024)
        except HTTPError as exc:
            # Only an authentic Twirp not_found acknowledgement is accepted
            # as absence; a reverse-proxy's arbitrary 404 is not success.
            if exc.code == 404:
                try:
                    payload = json.loads(exc.read(2048))
                    if payload.get("code") == "not_found":
                        return
                except (ValueError, TypeError, AttributeError):
                    pass
            raise MediaTeardownError("livekit_staff_remove_http_failed") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise MediaTeardownError("livekit_staff_remove_unreachable") from exc
