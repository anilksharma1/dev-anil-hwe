"""The frozen-NR check must be newline-agnostic.

Windows reported DRIFT on detection.py AND routing.py at once on the first real run. Both
"now" hashes turned out to be the CRLF rendering of the unchanged LF files -- the zip ships
LF, something on the Windows side converted. Nothing about the code had changed.

That class of false alarm is worse than no check at all: a DRIFT on the recall-critical path
either gets ignored, or gets silenced with `capture` -- which is precisely how a REAL
regression would get blessed into the golden. So the whole-file hash normalises line endings,
and these tests keep it that way while proving real edits are still caught.
"""
import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
sys.path.insert(0, _TOOLS)

import check_nr_frozen as cf  # noqa: E402


class TestNewlineAgnostic(unittest.TestCase):
    def test_crlf_and_lf_hash_identically(self):
        lf = b"line one\nline two\nline three\n"
        crlf = b"line one\r\nline two\r\nline three\r\n"
        cr = b"line one\rline two\rline three\r"
        h = lambda b: hashlib.sha256(cf._normalize_newlines(b)).hexdigest()
        self.assertEqual(h(lf), h(crlf), "CRLF must hash the same as LF")
        self.assertEqual(h(lf), h(cr), "old-Mac CR must hash the same as LF")

    def test_mixed_endings_normalise(self):
        mixed = b"a\r\nb\nc\rd\n"
        self.assertEqual(cf._normalize_newlines(mixed), b"a\nb\nc\nd\n")

    def test_crlf_conversion_does_not_change_the_frozen_hash(self):
        """The real scenario: read a frozen file, convert it to CRLF, confirm the hash holds."""
        for rel in cf._WHOLE_FILE:
            with open(os.path.join(cf._PKG, rel), "rb") as fh:
                raw = fh.read()
            as_lf = raw.replace(b"\r\n", b"\n")
            as_crlf = as_lf.replace(b"\n", b"\r\n")
            h = lambda b: hashlib.sha256(cf._normalize_newlines(b)).hexdigest()
            self.assertEqual(h(as_lf), h(as_crlf), rel)


class TestStillCatchesRealChanges(unittest.TestCase):
    """Newline-agnostic must not mean change-blind."""

    def _h(self, b):
        return hashlib.sha256(cf._normalize_newlines(b)).hexdigest()

    def test_added_line_is_caught(self):
        base = b"def f():\n    return 1\n"
        self.assertNotEqual(self._h(base), self._h(base + b"# a comment\n"))

    def test_removed_line_is_caught(self):
        base = b"a = 1\nb = 2\nc = 3\n"
        self.assertNotEqual(self._h(base), self._h(b"a = 1\nc = 3\n"))

    def test_one_token_edit_is_caught(self):
        """The change that actually matters: widening the strong-identifier set would let the
        AI clear files it currently can never clear."""
        base = b'STRONG_KEYS = ("SSN",)\n'
        edited = b'STRONG_KEYS = ("SSN", "CARD")\n'
        self.assertNotEqual(self._h(base), self._h(edited))

    def test_whitespace_edit_is_caught(self):
        """Indentation is semantic in Python, so this must NOT be normalised away."""
        base = b"if x:\n    return 1\n"
        self.assertNotEqual(self._h(base), self._h(b"if x:\n        return 1\n"))


class TestFrozenScope(unittest.TestCase):
    def test_pins_the_five_expected_surfaces(self):
        fp = cf._fingerprint()
        self.assertEqual(sorted(fp["files"]), ["detection.py", "routing.py"])
        self.assertEqual(sorted(fp["functions"]),
                         ["azure_clients.llm_classify", "enrich.apply_llm"])
        self.assertEqual(sorted(fp["constants"]), ["azure_clients._SYSTEM_PROMPT"])

    def test_current_code_matches_the_golden(self):
        """The build ships green. If this fails, something modified the NR path."""
        self.assertEqual(cf.check(), 0, "frozen NR check is not passing on the shipped tree")


if __name__ == "__main__":
    unittest.main()
