"""Tests for conversion.py's timeout enforcement (Windows-only module; skipped elsewhere).

_run_with_timeout is exercised directly with plain Python callables instead of real
Word/Excel/PowerPoint COM calls, so these run without Office installed -- only pywin32
itself (a real production dependency, see requirements-windows.txt) is needed for
pythoncom.CoInitialize(). The image-name process-kill path uses a name no real process
will ever match, so it's a safe no-op here; what's under test is the timeout DETECTION
and exception routing, which conversion.py itself is responsible for regardless of what
process (if any) actually gets force-killed.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

try:
    import pythoncom  # noqa: F401
    HAVE_PYWIN32 = True
except ImportError:
    HAVE_PYWIN32 = False


@unittest.skipUnless(sys.platform == "win32", "conversion.py is Windows-only")
@unittest.skipUnless(HAVE_PYWIN32, "pywin32 not installed")
class RunWithTimeout(unittest.TestCase):
    def setUp(self):
        from pii_triage.conversion import _run_with_timeout, ConversionTimeout
        self._run_with_timeout = _run_with_timeout
        self.ConversionTimeout = ConversionTimeout

    def test_fast_call_returns_its_value(self):
        result = self._run_with_timeout(lambda: "ok", "NO_SUCH_IMAGE.EXE", timeout_s=5)
        self.assertEqual(result, "ok")

    def test_hung_call_raises_conversion_timeout_not_returns_none(self):
        def hang():
            time.sleep(3)
            return "too late"
        with self.assertRaises(self.ConversionTimeout):
            self._run_with_timeout(hang, "NO_SUCH_IMAGE.EXE", timeout_s=0.2)

    def test_ordinary_exception_propagates_as_itself(self):
        def boom():
            raise ValueError("corrupt file")
        with self.assertRaises(ValueError):
            self._run_with_timeout(boom, "NO_SUCH_IMAGE.EXE", timeout_s=5)


@unittest.skipUnless(sys.platform == "win32", "conversion.py is Windows-only")
@unittest.skipUnless(HAVE_PYWIN32, "pywin32 not installed")
class ConvertLegacyOfficeTimeoutRouting(unittest.TestCase):
    """convert_legacy_office must let ConversionTimeout escape (so worker.py can treat a
    lost cause differently), while an ordinary converter failure still degrades to None."""

    def setUp(self):
        import pii_triage.conversion as conversion
        self.conversion = conversion
        self._orig_converters = dict(conversion._CONVERTERS)

    def tearDown(self):
        self.conversion._CONVERTERS.update(self._orig_converters)

    def test_timeout_escapes_as_conversion_timeout(self):
        def hang(src, dest, dest_path):
            time.sleep(3)
        self.conversion._CONVERTERS[".doc"] = hang
        with self.assertRaises(self.conversion.ConversionTimeout):
            self.conversion.convert_legacy_office("x.doc", __import__("pathlib").Path("."),
                                                   timeout_s=0.2)

    def test_ordinary_failure_returns_none(self):
        def boom(src, dest, dest_path):
            raise RuntimeError("could not open")
        self.conversion._CONVERTERS[".doc"] = boom
        result = self.conversion.convert_legacy_office(
            "x.doc", __import__("pathlib").Path("."), timeout_s=5)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
