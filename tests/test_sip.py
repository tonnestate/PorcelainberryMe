"""Offline SIP control tests: fake baresip 0.6.0, no SIP network or call."""
import asyncio
from io import BytesIO
from types import SimpleNamespace
import unittest
import wave

from porcelainberryme.sip import SipCredentials, SipLine


def wav_bytes(rate=8000, channels=1, frames=160):
    data = BytesIO()
    with wave.open(data, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\0\0" * frames * channels)
    return data.getvalue()


class FakeCall:
    def __init__(self, peer="sip:caller@example.test"):
        self.peer = peer
        self.answered = False
        self.hung_up = False
        self.listeners = []
        self.frames_written = []
        self.audio = self
        self.info_value = SimpleNamespace(tx_sample_rate=8000, tx_channels=1)

    def info(self):
        return self.info_value

    def read(self, size):
        return b"\x00\x01"

    def write(self, data):
        self.frames_written.append(data)
        return len(data)

    def on(self, callback):
        self.listeners.append(callback)

    async def answer(self):
        self.answered = True

    async def wait_established(self):
        return None

    async def hangup(self):
        self.hung_up = True
        for listener in self.listeners:
            listener(SimpleNamespace(event="closed"))


class FakeBackend:
    __version__ = "0.6.0"
    Event = SimpleNamespace(CALL_CLOSED="closed")

    def __init__(self):
        self.ua = None
        self.runtime = None

    def Runtime(self):
        backend = self
        class Runtime:
            async def start(self, conf):
                backend.runtime = self
                self.started = True
            async def close(self):
                self.closed = True
        return Runtime()

    def Config(self):
        return object()

    def Account(self, *, user, password, domain):
        return SimpleNamespace(user=user, password=password, domain=domain)

    @property
    def UserAgent(self):
        backend = self
        class Agent:
            @classmethod
            async def create(cls, runtime, account):
                backend.ua = cls()
                backend.ua.account = account
                return backend.ua

            def on_incoming(self, callback):
                self.handler = callback

            async def register(self):
                self.registered = True

            async def unregister(self):
                self.registered = False

            async def dial(self, destination):
                self.last_dial = destination
                return FakeCall(destination)
        return Agent


class SipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.creds = SipCredentials(user="1001", password="password", domain="sip.example.test")

    def line(self, incoming=lambda event: False, outgoing=None):
        return SipLine(self.creds, tenant_id="tenant-a",
                       authorize_incoming=incoming, authorize_outgoing=outgoing,
                       backend=self.backend)

    async def test_not_open_until_explicitly_started(self):
        line = self.line()
        self.assertIsNone(self.backend.ua)
        with self.assertRaises(RuntimeError):
            await line.next_incoming()
        await line.open()
        self.assertTrue(self.backend.ua.registered)
        await line.close()
        self.assertFalse(self.backend.ua.registered)
        self.assertTrue(self.backend.runtime.closed)

    async def test_denied_inbound_is_hung_up_not_answered(self):
        line = await self.line().__aenter__()
        try:
            call = FakeCall()
            self.backend.ua.handler(call)
            result = await line.next_incoming(timeout=1)
            self.assertIsNone(result)
            self.assertTrue(call.hung_up)
            self.assertFalse(call.answered)
        finally:
            await line.close()

    async def test_inbound_authorized_and_conversation_media(self):
        seen = []
        line = await self.line(incoming=lambda event: seen.append(event) or True).open()
        try:
            call = FakeCall()
            self.backend.ua.handler(call)
            session = await line.next_incoming(timeout=1)
            self.assertTrue(call.answered)
            self.assertEqual(seen[0].channel, "phone")
            self.assertEqual(seen[0].kind, "call_started")
            self.assertEqual(seen[0].tenant_id, "tenant-a")
            self.assertEqual(session.read_pcm(4), b"\x00\x01")
            self.assertEqual(session.write_pcm(b"\x01\x02"), 2)
            with self.assertRaises(ValueError):
                session.write_pcm(b"X")
            self.assertEqual(await session.play_wav(wav_bytes()), 320)
            with self.assertRaisesRegex(ValueError, "sip_wav_format_mismatch"):
                await session.play_wav(wav_bytes(rate=16000))
            await session.hangup()
            self.assertTrue(call.hung_up)
        finally:
            await line.close()

    async def test_one_active_call_no_parallel_answer(self):
        line = await self.line(incoming=lambda event: True).open()
        try:
            first = FakeCall()
            second = FakeCall("sip:other@example.test")
            self.backend.ua.handler(first)
            self.backend.ua.handler(second)
            await line.next_incoming(timeout=1)
            self.assertIsNone(await line.next_incoming(timeout=1))
            self.assertTrue(second.hung_up)
            self.assertFalse(second.answered)
        finally:
            await line.close()

    async def test_outbound_denied_by_default(self):
        line = await self.line(incoming=lambda event: False).open()
        try:
            with self.assertRaisesRegex(PermissionError, "sip_outbound_not_authorized"):
                await line.dial("sip:123@example.test")
            self.assertFalse(hasattr(self.backend.ua, "last_dial"))
        finally:
            await line.close()

    async def test_supervised_outbound_dial(self):
        line = await self.line(outgoing=lambda uri: uri == "sip:200@example.test").open()
        try:
            session = await line.dial("sip:200@example.test")
            self.assertEqual(session.identity.direction, "outgoing")
            self.assertEqual(self.backend.ua.last_dial, "sip:200@example.test")
            with self.assertRaisesRegex(RuntimeError, "sip_line_busy"):
                await line.dial("sip:200@example.test")
            with self.assertRaises(ValueError):
                await line.dial("https://example.org")
        finally:
            await line.close()

    def test_invalid_credentials(self):
        with self.assertRaises(ValueError):
            SipCredentials(user="bad @", password="p", domain="example.test")


if __name__ == "__main__":
    unittest.main()
