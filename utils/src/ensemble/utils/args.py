"""Once rejects repeated command-line options."""

import argparse


class Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest) is not None:
            parser.error(f"{option_string} given twice")
        setattr(namespace, self.dest, values)
