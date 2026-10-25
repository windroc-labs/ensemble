"""utils/args.py: Once."""

import argparse
import io
import unittest
from unittest import mock

from ensemble.utils.args import Once

from utils.tests.case import Case


class OnceTest(Case):
    def parser(self):
        ap = argparse.ArgumentParser(prog="p")
        ap.add_argument("--dir", action=Once)
        return ap

    def test_given_once_is_its_value(self):
        self.assertEqual(self.parser().parse_args(["--dir", "/a"]).dir, "/a")
        self.assertIsNone(self.parser().parse_args([]).dir)

    def test_given_twice_is_an_argparse_error(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err), self.assertRaises(SystemExit) as cm:
            self.parser().parse_args(["--dir", "/a", "--dir=/b"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("p: error: --dir given twice", err.getvalue())


if __name__ == "__main__":
    unittest.main()
