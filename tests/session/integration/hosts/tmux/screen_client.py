"""Deterministic inline and alternate-screen output for real guard displays."""

import os
import tty


def main():
    tty.setraw(0)
    os.write(1, b"READY")
    pending = b""
    while data := os.read(0, 4096):
        pending += data
        while b"\n" in pending:
            command, pending = pending.split(b"\n", 1)
            if command in (b"inline", b"alt"):
                wire = b"\x1b[?1049l\x1b[r\x1b[2J\x1b[H"
                if command == b"alt":
                    wire += b"main\x1b[2;3H\x1b[?1049h"
                wire += b"top\r\na\r\nb\r\nc\r\nfooter\x1b[2;4r\x1b[4;1H"
                os.write(1, wire)
            elif command == b"scroll":
                os.write(1, b"\r\nnew")
            elif command == b"normal":
                os.write(1, b"\x1b[?1049lX")
            elif command == b"history":
                os.write(1, b"\x1b[?1049l\x1b[r\x1b[2J\x1b[H")
                for number in range(1, 121):
                    os.write(1, f"PROOF-{number:03}\r\n".encode())
                os.write(1, b"done")


if __name__ == "__main__":
    main()
