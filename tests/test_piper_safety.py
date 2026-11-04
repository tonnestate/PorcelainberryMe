"""Real subprocess regression tests for self-hosted Piper process safety.

Uses a temporary Python executable as a Piper stand-in. No model weights,
audio device, real user speech, cloud account, or external API is needed.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from porcelainberryme.local_voice import LocalPiperVoice, LocalVoiceUnavailable


class PiperProcessSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.binary = self.directory / "piper"
        self.model = self.directory / "voice.onnx"
        self.model.write_bytes(b"test-model-not-actually-loaded")
        Path(str(self.model) + ".json").write_text(
            json.dumps({"audio": {"sample_rate": 16000}}),
            encoding="utf-8",
        )

    def write_executable(self, body: str):
        self.binary.write_text(
            "#!/usr/bin/env python3\n" + body + "\n",
            encoding="utf-8",
        )
        self.binary.chmod(0o700)

    async def test_valid_piper_raw_pcm_still_works(self):
        self.write_executable(
            "import sys\n"
            "assert sys.stdin.buffer.readline().strip() == b'Hallo'\n"
            "sys.stdout.buffer.write(bytes([4, 0]) * 320)\n",
        )
        adapter = LocalPiperVoice(str(self.binary), str(self.model), timeout=3)
        packet = await adapter.synthesize("Hallo")
        self.assertEqual(packet.sample_rate, 16000)
        self.assertEqual(packet.pcm, bytes([4, 0]) * 320)

    async def test_progressive_piper_emits_pcm_before_child_finishes(self):
        completed = self.directory / "finished.txt"
        self.write_executable(
            "import sys, time\n"
            "sys.stdin.buffer.read()\n"
            "sys.stdout.buffer.write(bytes([4, 0]) * 1600)\n"
            "sys.stdout.buffer.flush()\n"
            "time.sleep(0.25)\n"
            "sys.stdout.buffer.write(bytes([5, 0]) * 1600)\n"
            "sys.stdout.buffer.flush()\n"
            f"open({str(completed)!r}, 'w').write('finished')\n"
        )
        with patch.dict(os.environ, {"PB_LOCAL_PIPER_STREAM": "1"}):
            adapter = LocalPiperVoice(str(self.binary), str(self.model), timeout=3)
            self.assertTrue(adapter.streaming_enabled)
            stream = adapter.stream_audio("Hallo")
            first = await asyncio.wait_for(anext(stream), timeout=2)
            self.assertEqual(first.pcm, bytes([4, 0]) * 1600)
            self.assertFalse(completed.exists())
            second = await asyncio.wait_for(anext(stream), timeout=2)
            self.assertEqual(second.pcm, bytes([5, 0]) * 1600)
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)
            self.assertTrue(completed.exists())

    async def test_closing_partial_piper_stream_kills_native_process(self):
        pid_file = self.directory / "speech.pid"
        self.write_executable(
            "import os, sys, time\n"
            f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "sys.stdin.buffer.read()\n"
            "sys.stdout.buffer.write(bytes([4, 0]) * 1600)\n"
            "sys.stdout.buffer.flush()\n"
            "time.sleep(10)\n"
        )
        adapter = LocalPiperVoice(str(self.binary), str(self.model), timeout=3)
        stream = adapter.stream_audio("Hallo")
        first = await asyncio.wait_for(anext(stream), timeout=2)
        self.assertEqual(len(first.pcm), 3200)
        pid = int(pid_file.read_text())
        await asyncio.wait_for(stream.aclose(), timeout=2)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_oversized_piper_output_is_rejected_during_read(self):
        self.write_executable(
            "import sys\n"
            "sys.stdin.buffer.read()\n"
            "sys.stdout.buffer.write(bytes([1, 2]) * 4096)\n",
        )
        adapter = LocalPiperVoice(str(self.binary), str(self.model), timeout=3)
        with patch("porcelainberryme.local_voice.MAX_REPLY_BYTES", 1024):
            with self.assertRaisesRegex(
                LocalVoiceUnavailable, "local_piper_output_too_large"
            ):
                await adapter.synthesize("Hallo")

    async def test_hanging_piper_is_killed_on_timeout(self):
        self.write_executable(
            "import sys, time\n"
            "sys.stdin.buffer.read()\n"
            "time.sleep(10)\n",
        )
        adapter = LocalPiperVoice(str(self.binary), str(self.model), timeout=0.2)
        with self.assertRaisesRegex(LocalVoiceUnavailable, "local_piper_timeout"):
            await asyncio.wait_for(adapter.synthesize("Hallo"), timeout=3)

    async def test_trickling_piper_output_cannot_extend_total_deadline(self):
        self.write_executable(
            "import sys, time\n"
            "sys.stdin.buffer.read()\n"
            "for _ in range(100):\n"
            "    sys.stdout.buffer.write(bytes([4, 0]) * 1600)\n"
            "    sys.stdout.buffer.flush()\n"
            "    time.sleep(0.045)\n"
        )
        adapter = LocalPiperVoice(str(self.binary), str(self.model), timeout=0.3)
        with self.assertRaisesRegex(LocalVoiceUnavailable, "local_piper_timeout"):
            await asyncio.wait_for(adapter.synthesize("Hallo"), timeout=2)

    async def test_cancelled_synthesis_does_not_leave_worker_running(self):
        self.write_executable(
            "import sys, time\n"
            "sys.stdin.buffer.read()\n"
            "time.sleep(10)\n",
        )
        adapter = LocalPiperVoice(str(self.binary), str(self.model), timeout=30)
        job = asyncio.create_task(adapter.synthesize("Hallo"))
        await asyncio.sleep(0.1)
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(job, timeout=3)

    async def test_nonzero_exit_code_is_not_presented_as_speech(self):
        self.write_executable(
            "import sys\n"
            "sys.stdin.buffer.read()\n"
            "sys.exit(3)\n",
        )
        adapter = LocalPiperVoice(str(self.binary), str(self.model), timeout=3)
        with self.assertRaisesRegex(LocalVoiceUnavailable, "local_piper_failed"):
            await adapter.synthesize("Hallo")


if __name__ == "__main__":
    unittest.main()
