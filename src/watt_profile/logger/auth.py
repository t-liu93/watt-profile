"""Bounded process-local operator sessions and independent CSRF state."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

COOKIE = "watt_logger_session"
LOGIN_COOKIE = "watt_logger_login"
MAX_AGE = 8 * 60 * 60


@dataclass(frozen=True)
class Session:
    csrf: str
    created: float


class OperatorAuth:
    def __init__(self, username: str, password: str, clock: Callable[[], float] = time.monotonic):
        self._username = hashlib.sha256(username.encode()).digest()
        self._password = hashlib.sha256(password.encode()).digest()
        self.clock = clock
        self.sessions: OrderedDict[str, Session] = OrderedDict()
        self.prelogin: OrderedDict[str, Session] = OrderedDict()
        self.attempts: OrderedDict[str, list[float]] = OrderedDict()

    def _new(self, states, capacity):
        identifier = secrets.token_urlsafe(32)
        state = Session(secrets.token_urlsafe(32), self.clock())
        states[identifier] = state
        while len(states) > capacity:
            states.popitem(last=False)
        return identifier, state

    def login_state(self):
        return self._new(self.prelogin, 256)

    def session(self, identifier):
        state = self.sessions.get(identifier)
        if state and self.clock() - state.created >= MAX_AGE:
            self.sessions.pop(identifier, None)
            return None
        return state

    def valid_csrf(self, identifier, token, *, login=False):
        state = self.prelogin.get(identifier) if login else self.session(identifier)
        lifetime = 60 * 15 if login else MAX_AGE
        return bool(
            state
            and self.clock() - state.created < lifetime
            and isinstance(token, str)
            and hmac.compare_digest(state.csrf.encode(), token.encode())
        )

    def allow_attempt(self, address):
        now = self.clock()
        recent = [t for t in self.attempts.pop(address, []) if now - t < 60]
        allowed = len(recent) < 10
        if allowed:
            recent.append(now)
        self.attempts[address] = recent
        while len(self.attempts) > 1024:
            self.attempts.popitem(last=False)
        return allowed

    def authenticate(self, username, password):
        user_ok = hmac.compare_digest(self._username, hashlib.sha256(username.encode()).digest())
        password_ok = hmac.compare_digest(
            self._password, hashlib.sha256(password.encode()).digest()
        )
        return user_ok & password_ok

    def issue(self):
        return self._new(self.sessions, 32)

    def clear(self):
        self.sessions.clear()
        self.prelogin.clear()
        self.attempts.clear()
