from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from porcelainberryme.preflight import collect, disk_info, voice_info, unit_info


class ReadOnlyPreflightTests(unittest.TestCase):
    def test_reads_disk_without_scanning_tree(self):
        with tempfile.TemporaryDirectory() as d:
            info = disk_info(d)
            self.assertTrue(info["available"])
            self.assertGreater(info["total_bytes"], 0)
            self.assertGreaterEqual(info["free_bytes"], 0)

    def test_explicit_voice_paths_only(self):
        self.assertEqual(voice_info(None, None), {"configured": False})
        self.assertEqual(voice_info("relative", "/model.onnx")["reason"],
                         "absolute_paths_required")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            exe = path / "piper"
            exe.write_text("x", encoding="utf-8")
            exe.chmod(0o700)
            model = path / "de_DE.onnx"
            model.write_bytes(b"test")
            (path / "de_DE.onnx.json").write_text("{}", encoding="utf-8")
            report = voice_info(str(exe), str(model))
            self.assertTrue(report["model_exists"])
            self.assertTrue(report["model_config_exists"])
            self.assertEqual(report["model_bytes"], 4)

    def test_reports_only_service_states(self):
        class Fake:
            returncode = 0
            stdout = "LoadState=loaded\nActiveState=inactive\nOtherSecret=should-not-print\n"
        with patch("porcelainberryme.preflight.subprocess.run", return_value=Fake()) as call:
            report = unit_info("tonnestate-voice-gateway.service")
        self.assertEqual(report, {"available": True, "LoadState": "loaded",
                                  "ActiveState": "inactive"})
        self.assertEqual(call.call_args.args[0][:2], ["systemctl", "show"])
        with self.assertRaises(ValueError):
            unit_info("evil; cat /etc/passwd")

    def test_collection_has_no_mutating_side_effects(self):
        info = collect(disk_paths=("/",))
        self.assertEqual(info["mode"], "read_only")
        self.assertIn("disk", info)
        self.assertFalse(info["voice"]["configured"])


if __name__ == "__main__":
    unittest.main()
