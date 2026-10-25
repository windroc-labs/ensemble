"""Roster event files and recoverable rotation under the caller's stable file lock."""

import hashlib
import json
import os
import re
import stat
import time
from dataclasses import asdict, dataclass, replace

from ensemble.utils import append_line, read_regular, save, sync_directory

EVENTS_MAX = 1 << 20
SETTLE = 1


@dataclass(frozen=True)
class _Rotation:
    gen: int
    original: tuple[int, int]
    candidate: tuple[int, int] | None = None
    prefix_size: int = 0
    prefix_hash: str = ""
    rollback: tuple[int, int] | None = None


def _identity(path: str) -> tuple[int, int]:
    opened = os.stat(path)
    if not stat.S_ISREG(opened.st_mode):
        raise OSError(f"{path}: not a regular file")
    return opened.st_dev, opened.st_ino


def _file_id(value) -> tuple[int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(type(number) is not int or number < 0 for number in value)
    ):
        raise OSError("events rotation: invalid file identity")
    return value[0], value[1]


def _opened(path: str) -> int | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(f"{path}: not a regular file")
    return fd


def _drain(fd: int | None) -> bytes:
    chunks = []
    while fd is not None and (chunk := os.read(fd, 1 << 16)):
        chunks.append(chunk)
    return b"".join(chunks)


def _read(path: str) -> bytes:
    data = read_regular(path)
    if data is None:
        raise OSError(f"{path}: not a readable regular file")
    return data


class Events:
    def __init__(self, directory: str):
        self.path = os.path.join(directory, "events")
        self.archive = self.path + ".1"
        self.journal = self.path + ".rotation.json"
        self.candidate = self.path + ".next"
        self.rollback = self.path + ".rollback"

    def identity(self) -> tuple[int, int] | None:
        try:
            return _identity(self.path)
        except FileNotFoundError:
            return None

    def ensure(self) -> None:
        fd = os.open(
            self.path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK,
            0o600,
        )
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError(f"{self.path}: not a regular file")
            os.fsync(fd)
        finally:
            os.close(fd)
        sync_directory(os.path.dirname(self.path))

    def read(self) -> bytes:
        data = read_regular(self.path)
        if data is None:
            if os.path.lexists(self.path):
                raise OSError(f"{self.path}: not a readable regular file")
            return b""
        return data

    def following(self, after: int) -> list[str]:
        data = self.read().split(b"\n")[:-1]
        return [line.decode(errors="replace") for line in data[after:]]

    def count(self) -> int:
        return self.read().count(b"\n")

    def _due(self, data: bytes, cursor: int) -> bool:
        retained = data.split(b"\n", cursor)[-1]
        return len(data) > EVENTS_MAX and len(data) - len(retained) >= EVENTS_MAX // 2

    def due(self, cursor: int) -> bool:
        return self._due(self.read(), cursor)

    def _intent(self) -> _Rotation | None:
        raw = read_regular(self.journal, limit=65537)
        if raw is None:
            if os.path.lexists(self.journal):
                raise OSError(f"{self.journal}: not a readable regular file")
            return None
        if len(raw) > 65536:
            raise OSError(f"{self.journal}: oversized rotation journal")
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise OSError(f"{self.journal}: invalid rotation journal") from error
        if not isinstance(data, dict) or set(data) != {
            "gen",
            "original",
            "candidate",
            "prefix_size",
            "prefix_hash",
            "rollback",
        }:
            raise OSError(f"{self.journal}: invalid rotation fields")
        if type(data["gen"]) is not int or data["gen"] < 0:
            raise OSError(f"{self.journal}: invalid rotation generation")
        if type(data["prefix_size"]) is not int or data["prefix_size"] < 0:
            raise OSError(f"{self.journal}: invalid rotation prefix size")
        candidate = None if data["candidate"] is None else _file_id(data["candidate"])
        if candidate is None:
            if data["prefix_size"] != 0 or data["prefix_hash"] != "":
                raise OSError(f"{self.journal}: unprepared rotation prefix")
        elif not isinstance(data["prefix_hash"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", data["prefix_hash"]
        ):
            raise OSError(f"{self.journal}: invalid rotation prefix hash")
        return _Rotation(
            data["gen"],
            _file_id(data["original"]),
            candidate,
            data["prefix_size"],
            data["prefix_hash"],
            None if data["rollback"] is None else _file_id(data["rollback"]),
        )

    def _save_intent(self, rotation: _Rotation) -> None:
        save(self.journal, json.dumps(asdict(rotation)) + "\n", durable=True)

    def _cleanup(self) -> None:
        for path in (self.candidate, self.rollback, self.journal):
            if os.path.lexists(path):
                os.unlink(path)
        sync_directory(os.path.dirname(self.path))

    def _suffix(self, rotation: _Rotation, identity: tuple[int, int] | None) -> bytes:
        data = b"" if identity is None else _read(self.path)
        if rotation.candidate is None or identity != rotation.candidate:
            return data
        prefix = data[: rotation.prefix_size]
        if (
            len(prefix) != rotation.prefix_size
            or hashlib.sha256(prefix).hexdigest() != rotation.prefix_hash
        ):
            raise OSError(f"{self.path}: rotation prefix changed")
        return data[rotation.prefix_size :]

    def recover(self, gen: int) -> None:
        rotation = self._intent()
        if rotation is None:
            return
        identity = _identity(self.path) if os.path.lexists(self.path) else None
        if gen == rotation.gen + 1:
            if (
                rotation.candidate is None
                or identity != rotation.candidate
                or _identity(self.archive) != rotation.original
            ):
                raise OSError(f"{self.journal}: committed rotation files changed")
            self._suffix(rotation, identity)
            self._cleanup()
            return
        if gen != rotation.gen:
            raise OSError(f"{self.journal}: rotation generation differs from ledger")
        if identity == rotation.original or (
            rotation.rollback is not None and identity == rotation.rollback
        ):
            self._cleanup()
            return
        if _identity(self.archive) != rotation.original:
            raise OSError(f"{self.archive}: rotation archive changed")
        restored = _read(self.archive) + self._suffix(rotation, identity)
        save(self.rollback, restored, durable=True)
        rotation = replace(rotation, rollback=_identity(self.rollback))
        self._save_intent(rotation)
        os.replace(self.rollback, self.path)
        sync_directory(os.path.dirname(self.path))
        self._cleanup()

    def _keep(self, late: bytes) -> None:
        """Lines the replaced fresh file took while the rotation published: appended to the events
        file, else to its archive.
        """
        if not late:
            return
        try:
            append_line(self.path, late, durable=True)
        except OSError as error:
            try:
                append_line(self.archive, late, durable=True)
            except OSError:
                raise OSError(f"{error}: {len(late)} bytes written while rotating lost") from error
            raise OSError(
                f"{error}: lines written while rotating retained in {self.archive}"
            ) from error

    def rotate(self, cursor: int, gen: int) -> bool:
        self.recover(gen)
        data = self.read()
        if not self._due(data, cursor):
            return False
        self.ensure()
        rotation = _Rotation(gen, _identity(self.path))
        self._save_intent(rotation)
        os.replace(self.path, self.archive)
        fresh = None
        try:
            try:
                sync_directory(os.path.dirname(self.path))
                self.ensure()
                time.sleep(SETTLE)
                tail = _read(self.archive).split(b"\n", cursor)[-1]
                if tail and not tail.endswith(b"\n"):
                    tail += b"\n"
                fresh = _opened(self.path)
                save(self.candidate, tail + _drain(fresh), durable=True)
                rotation = replace(
                    rotation,
                    candidate=_identity(self.candidate),
                    prefix_size=len(tail),
                    prefix_hash=hashlib.sha256(tail).hexdigest(),
                )
                self._save_intent(rotation)
                os.replace(self.candidate, self.path)
                sync_directory(os.path.dirname(self.path))
            except OSError as error:
                raise OSError(
                    f"{error}: unread lines after {cursor} retained in {self.archive}"
                ) from error
            time.sleep(SETTLE)  # a writer that opened the fresh file before the publish
            self._keep(_drain(fresh))
        finally:
            if fresh is not None:
                os.close(fresh)
        return True
