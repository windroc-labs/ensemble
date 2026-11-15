#!/usr/bin/env python3
"""An ensemble-broker for the host gh wrapper's tests: logs each call's arguments to $FAKE_BROKER_LOG and
replies as host-gh-login does, with a token expiring in an hour."""

import json
import os
import sys
import time
from datetime import UTC, datetime


def main():
    with open(os.environ["FAKE_BROKER_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps(sys.argv[1:]) + "\n")
    expires = datetime.fromtimestamp(time.time() + 3600, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(json.dumps({"version": 1, "account": "windroc-labs", "expires_at": expires}))


if __name__ == "__main__":
    main()
