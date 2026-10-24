"""Signing in to the web UI: one owner, one password (DESIGN.md, section 12).

- The first time, the owner proves that the instance is theirs with a one-time setup code that only the service's
  log shows, and chooses the password. `lastcall password reset` forgets it, and the log shows a new code.
- The password is kept as an scrypt hash in the secret store.
- A session is a random token in an HttpOnly cookie. The store keeps only the token's SHA-256, with the session's
  CSRF token. A session ends after 30 days, or after 7 days without use.
- Failed attempts lock: 5 from one address in 15 minutes lock that address, and 20 from anywhere lock every address,
  for 15 minutes after the last failure.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from collections.abc import Callable
from typing import Any

from lastcall.secretstore import SecretStore
from lastcall.store import Store

SECRET = "web-password"
MIN_LENGTH, MAX_LENGTH = 12, 256
SESSION_DAYS, IDLE_DAYS = 30, 7
TOUCH_EVERY = 300.0  # a session's last use is written at most this often
LOCK_SECONDS = 15 * 60.0
LOCK_ADDRESS, LOCK_ALL = 5, 20
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
MAXMEM = 64 * 1024 * 1024
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0, O, 1 or I
DAY = 86_400.0


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=MAXMEM, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(key)}"


def check_password(stored: str, password: str) -> bool:
    try:
        kind, n, r, p, salt, key = stored.split("$")
        expected = base64.b64decode(key)
        given = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=MAXMEM,
            dklen=len(expected),
        )
    except ValueError:
        return False
    return kind == "scrypt" and hmac.compare_digest(given, expected)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class Owner:
    """The owner's password, in the secret store."""

    def __init__(self, store: SecretStore) -> None:
        self.store = store
        self.dummy = hash_password(secrets.token_urlsafe(16))

    def has_password(self) -> bool:
        return self.store.get(SECRET) is not None

    def set_password(self, password: str) -> None:
        self.store.set(SECRET, hash_password(password))

    def check(self, password: str) -> bool:
        stored = self.store.get(SECRET)
        ok = check_password(stored or self.dummy, password)  # the same work with or without a password
        return ok and stored is not None

    def reset(self) -> bool:
        return self.store.delete(SECRET)


def password_problem(password: str, repeated: str) -> tuple[str, dict[str, int]] | None:
    """The catalog key (and its values) that says what is wrong with a new password, or None."""
    if len(password) < MIN_LENGTH:
        return "web.setup.short", {"min": MIN_LENGTH}
    if len(password) > MAX_LENGTH:
        return "web.setup.long", {"max": MAX_LENGTH}
    if not hmac.compare_digest(password.encode(), repeated.encode()):
        return "web.setup.mismatch", {}
    return None


class SetupCode:
    """The one-time code that proves the instance is the owner's. Only the service's log shows it."""

    def __init__(self, log: Callable[[str], None]) -> None:
        self.log = log
        self.value: str | None = None

    def issue(self) -> None:
        if self.value is not None:
            return
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        self.value = f"{raw[:4]}-{raw[4:]}"
        self.log(f"Web UI setup code: {self.value} (open the web UI to choose your password)")

    def check(self, given: str) -> bool:
        if self.value is None:
            return False
        return hmac.compare_digest(_plain(given).encode(), _plain(self.value).encode())

    def clear(self) -> None:
        self.value = None


def _plain(code: str) -> str:
    return "".join(c for c in code.upper() if c.isalnum())


class Throttle:
    """Failed sign-ins and setup codes, per address."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock = clock
        self.failures: dict[str, list[float]] = {}

    def wait(self, address: str) -> float:
        """Seconds until `address` may try again; 0 when it may try now."""
        now = self.clock()
        self._forget_old(now)
        mine = self.failures.get(address, [])
        everyone = sorted(t for times in self.failures.values() for t in times)
        waits = [0.0]
        if len(mine) >= LOCK_ADDRESS:
            waits.append(mine[-1] + LOCK_SECONDS - now)
        if len(everyone) >= LOCK_ALL:
            waits.append(everyone[-1] + LOCK_SECONDS - now)
        return max(waits)

    def fail(self, address: str) -> None:
        self.failures.setdefault(address, []).append(self.clock())

    def forget(self, address: str) -> None:
        self.failures.pop(address, None)

    def _forget_old(self, now: float) -> None:
        for address in list(self.failures):
            recent = [t for t in self.failures[address] if now - t < LOCK_SECONDS]
            if recent:
                self.failures[address] = recent
            else:
                del self.failures[address]


def start_session(store: Store, now: float) -> str:
    """A new session; returns the token for the cookie."""
    token = secrets.token_urlsafe(32)
    store.drop_sessions(seen_before=now - IDLE_DAYS * DAY)
    store.add_session(_digest(token), secrets.token_urlsafe(32), now)
    return token


def find_session(store: Store, token: str, now: float) -> dict[str, Any] | None:
    row = store.session(_digest(token))
    if row is None:
        return None
    if now - row["created"] > SESSION_DAYS * DAY or now - row["seen"] > IDLE_DAYS * DAY:
        store.drop_session(row["token"])
        return None
    if now - row["seen"] > TOUCH_EVERY:
        store.touch_session(row["token"], now)
    return row


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
