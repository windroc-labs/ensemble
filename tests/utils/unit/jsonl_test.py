"""tests/utils/jsonl.py: read_jsonl, a whole JSONL file as a list."""

import json
from pathlib import Path

from tests.utils.case import Case
from tests.utils.jsonl import read_jsonl
from tests.utils.sandbox import scratch


class ReadJsonlTest(Case):
    def setUp(self):
        self.path = Path(scratch(self, real=False), "events.jsonl")

    def test_reads_each_line(self):
        self.path.write_text('{"event": "start"}\n[1, "é"]\n7\n', encoding="utf-8")
        self.assertEqual(read_jsonl(self.path), [{"event": "start"}, [1, "é"], 7])
        self.assertEqual(read_jsonl(str(self.path), missing_ok=True), read_jsonl(self.path))

    def test_missing_file_raises_unless_missing_ok(self):
        with self.assertRaises(FileNotFoundError):
            read_jsonl(self.path)
        self.assertEqual(read_jsonl(self.path, missing_ok=True), [])

    def test_blank_or_partial_line_raises(self):
        for text in ('{"a": 1}\n\n', '{"a": 1}\n{"b"'):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                with self.assertRaises(json.JSONDecodeError):
                    read_jsonl(self.path, missing_ok=True)
