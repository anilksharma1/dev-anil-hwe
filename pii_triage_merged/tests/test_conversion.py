"""Tests for conversion.py's LibreOffice-headless-based legacy Office conversion.

subprocess.run() and _soffice_binary() are monkeypatched directly, so these run without a
real LibreOffice install -- what's under test is conversion.py's OWN wrapper logic (argument
construction, profile isolation/cleanup, timeout routing, exit-code handling), not the actual
fidelity of a real soffice --convert-to run. See CLAUDE.md for the one real end-to-end check
this repo could not perform in the sandbox that wrote this: converting a genuine .doc/.xls/.ppt
and confirming python-docx/openpyxl/python-pptx can read the result -- validate that on a real
box before relying on this at scale.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import pii_triage.conversion as conversion


class SofficeBinaryLookup(unittest.TestCase):
    def setUp(self):
        self._orig_which = conversion.shutil.which
        self._orig_env = os.environ.get("SOFFICE_PATH")

    def tearDown(self):
        conversion.shutil.which = self._orig_which
        if self._orig_env is None:
            os.environ.pop("SOFFICE_PATH", None)
        else:
            os.environ["SOFFICE_PATH"] = self._orig_env

    def test_soffice_path_override_used_when_it_resolves(self):
        os.environ["SOFFICE_PATH"] = "/custom/soffice"
        conversion.shutil.which = lambda name: name if name == "/custom/soffice" else None
        self.assertEqual(conversion._soffice_binary(), "/custom/soffice")

    def test_falls_back_to_path_lookup_when_override_unset(self):
        os.environ.pop("SOFFICE_PATH", None)
        conversion.shutil.which = lambda name: "/usr/bin/soffice" if name == "soffice" else None
        self.assertEqual(conversion._soffice_binary(), "/usr/bin/soffice")

    def test_checks_libreoffice_name_too(self):
        os.environ.pop("SOFFICE_PATH", None)
        conversion.shutil.which = lambda name: "/usr/bin/libreoffice" if name == "libreoffice" else None
        self.assertEqual(conversion._soffice_binary(), "/usr/bin/libreoffice")

    def test_none_when_nothing_found(self):
        os.environ.pop("SOFFICE_PATH", None)
        conversion.shutil.which = lambda name: None
        self.assertIsNone(conversion._soffice_binary())


class ConvertLegacyOffice(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__import__("tempfile").mkdtemp())
        self.src = self.tmp / "report.doc"
        self.src.write_bytes(b"fake legacy office bytes")
        self.dest_dir = self.tmp / "out"

        self._orig_binary_fn = conversion._soffice_binary
        self._orig_run = conversion.subprocess.run
        conversion._soffice_binary = lambda: "/usr/bin/soffice"

    def tearDown(self):
        conversion._soffice_binary = self._orig_binary_fn
        conversion.subprocess.run = self._orig_run
        import shutil as _sh
        _sh.rmtree(self.tmp, ignore_errors=True)

    def test_unsupported_extension_returns_none_without_calling_soffice(self):
        txt = self.tmp / "a.txt"
        txt.write_text("x")
        called = []
        conversion.subprocess.run = lambda *a, **k: called.append(1)
        self.assertIsNone(conversion.convert_legacy_office(str(txt), self.dest_dir))
        self.assertEqual(called, [])

    def test_no_binary_found_returns_none(self):
        conversion._soffice_binary = lambda: None
        self.assertIsNone(conversion.convert_legacy_office(str(self.src), self.dest_dir))

    def test_successful_conversion_returns_dest_path(self):
        def fake_run(cmd, capture_output, text, timeout):
            # simulate soffice actually writing the converted file
            (self.dest_dir / "report.docx").parent.mkdir(parents=True, exist_ok=True)
            (self.dest_dir / "report.docx").write_bytes(b"docx bytes")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        conversion.subprocess.run = fake_run

        result = conversion.convert_legacy_office(str(self.src), self.dest_dir, timeout_s=30)
        self.assertEqual(result, self.dest_dir / "report.docx")

    def test_nonzero_exit_code_returns_none(self):
        conversion.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(
            a, 1, stdout="", stderr="soffice: unrecoverable error")
        self.assertIsNone(conversion.convert_legacy_office(str(self.src), self.dest_dir))

    def test_missing_output_file_returns_none_even_on_zero_exit(self):
        # soffice reported success but the file genuinely isn't there
        conversion.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
        self.assertIsNone(conversion.convert_legacy_office(str(self.src), self.dest_dir))

    def test_timeout_raises_conversion_timeout(self):
        def fake_run(cmd, capture_output, text, timeout):
            raise subprocess.TimeoutExpired(cmd, timeout)
        conversion.subprocess.run = fake_run
        with self.assertRaises(conversion.ConversionTimeout):
            conversion.convert_legacy_office(str(self.src), self.dest_dir, timeout_s=5)

    def test_command_targets_the_right_format_per_extension(self):
        seen = {}

        def fake_run(cmd, capture_output, text, timeout):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 1, "", "")   # fail fast, just inspect the argv
        conversion.subprocess.run = fake_run

        conversion.convert_legacy_office(str(self.src), self.dest_dir)
        cmd = seen["cmd"]
        self.assertIn("--convert-to", cmd)
        self.assertEqual(cmd[cmd.index("--convert-to") + 1], "docx")
        self.assertIn(os.path.abspath(str(self.src)), cmd)

    def test_profile_dir_is_created_and_cleaned_up(self):
        profile_dirs_seen = []
        real_mkdtemp = conversion.tempfile.mkdtemp

        def spying_mkdtemp(*a, **k):
            d = real_mkdtemp(*a, **k)
            profile_dirs_seen.append(d)
            return d
        conversion.tempfile.mkdtemp = spying_mkdtemp

        def fake_run(cmd, capture_output, text, timeout):
            return subprocess.CompletedProcess(cmd, 1, "", "")
        conversion.subprocess.run = fake_run

        try:
            conversion.convert_legacy_office(str(self.src), self.dest_dir)
        finally:
            conversion.tempfile.mkdtemp = real_mkdtemp

        self.assertEqual(len(profile_dirs_seen), 1)
        self.assertFalse(os.path.exists(profile_dirs_seen[0]), "profile dir must be cleaned up")

    def test_profile_dir_cleaned_up_even_on_timeout(self):
        profile_dirs_seen = []
        real_mkdtemp = conversion.tempfile.mkdtemp

        def spying_mkdtemp(*a, **k):
            d = real_mkdtemp(*a, **k)
            profile_dirs_seen.append(d)
            return d
        conversion.tempfile.mkdtemp = spying_mkdtemp

        def fake_run(cmd, capture_output, text, timeout):
            raise subprocess.TimeoutExpired(cmd, timeout)
        conversion.subprocess.run = fake_run

        try:
            with self.assertRaises(conversion.ConversionTimeout):
                conversion.convert_legacy_office(str(self.src), self.dest_dir, timeout_s=5)
        finally:
            conversion.tempfile.mkdtemp = real_mkdtemp

        self.assertFalse(os.path.exists(profile_dirs_seen[0]))


if __name__ == "__main__":
    unittest.main()
