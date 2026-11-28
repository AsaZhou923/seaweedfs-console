"""Bound password verification across API processes using short SQLite leases."""

from contextlib import contextmanager
import hashlib
import math
import time
import uuid

from . import db
from .security import AppError

WINDOW_SECONDS = 60
PEER_ATTEMPTS = 5
GLOBAL_ATTEMPTS = 30
CONCURRENT_ATTEMPTS = 2
LEASE_SECONDS = 30


@contextmanager
def admit(settings, peer: str):
    now = time.time()
    peer_hash = hashlib.sha256(peer.encode("utf-8")).hexdigest()
    lease = uuid.uuid4().hex
    retry_after = None
    with db.connect(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM login_admission_windows WHERE started_at<=?", (now - WINDOW_SECONDS,))
        conn.execute("DELETE FROM login_admission_leases WHERE expires_at<=?", (now,))
        for key, maximum in (("global", GLOBAL_ATTEMPTS), (peer_hash, PEER_ATTEMPTS)):
            row = conn.execute("SELECT started_at,attempts FROM login_admission_windows WHERE peer_hash=?", (key,)).fetchone()
            if row and row["attempts"] >= maximum:
                retry_after = max(1, math.ceil(row["started_at"] + WINDOW_SECONDS - now))
                break
        active = conn.execute("SELECT count(*),min(expires_at) FROM login_admission_leases").fetchone()
        if retry_after is None and active[0] >= CONCURRENT_ATTEMPTS:
            retry_after = max(1, math.ceil(active[1] - now))
        if retry_after is None:
            for key in ("global", peer_hash):
                conn.execute("""INSERT INTO login_admission_windows VALUES(?,?,1)
                    ON CONFLICT(peer_hash) DO UPDATE SET attempts=attempts+1""", (key, now))
            conn.execute("INSERT INTO login_admission_leases VALUES(?,?)", (lease, now + LEASE_SECONDS))
    if retry_after is not None:
        raise AppError("LOGIN_RATE_LIMITED", "Too many login attempts. Try again later.", 429,
                       detail={"retry_after": retry_after}, retryable=True)
    try:
        yield
    finally:
        with db.connect(settings) as conn:
            conn.execute("DELETE FROM login_admission_leases WHERE id=?", (lease,))
