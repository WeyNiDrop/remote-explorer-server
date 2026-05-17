from __future__ import annotations

import copy
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any

from .protocol import canonical_json

PBKDF2_ITERATIONS = 100_000
CHALLENGE_TTL_SECONDS = 120
SESSION_TTL_SECONDS = 24 * 60 * 60


class AuthError(ValueError):
    """Raised when authentication or command authorization fails."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class PendingChallenge:
    client_id: str
    server_nonce: str
    salt: str
    created_at: float


@dataclass
class Session:
    id: str
    client_id: str
    mode: str
    session_key: bytes | None
    created_at: float
    last_seen: float
    last_counter: int = 0


def random_hex(byte_count: int = 16) -> str:
    return secrets.token_hex(byte_count)


def derive_password_key(password: str, salt_hex: str, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        iterations,
    )


def hmac_hex(key: bytes, message: str) -> str:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()


def proof_message(client_id: str, server_nonce: str, client_nonce: str) -> str:
    return f"proof:{client_id}:{server_nonce}:{client_nonce}"


def session_message(client_id: str, server_nonce: str, client_nonce: str) -> str:
    return f"session:{client_id}:{server_nonce}:{client_nonce}"


def strip_signature(message: dict[str, Any]) -> dict[str, Any]:
    unsigned = copy.deepcopy(message)
    auth = unsigned.get("auth")
    if isinstance(auth, dict):
        auth.pop("signature", None)
    return unsigned


def sign_message(message: dict[str, Any], session_key: bytes) -> str:
    return hmac_hex(session_key, canonical_json(strip_signature(message)))


def verify_message_signature(message: dict[str, Any], session_key: bytes, signature: str) -> bool:
    expected = sign_message(message, session_key)
    return hmac.compare_digest(expected, signature)


class AuthManager:
    def __init__(self, password: str | None) -> None:
        self.password = password
        self._challenges: dict[str, PendingChallenge] = {}
        self._sessions: dict[str, Session] = {}

    @property
    def auth_mode(self) -> str:
        return "password" if self.password else "none"

    def start_auth(self, client: dict[str, Any]) -> dict[str, Any]:
        client_id = _client_id(client)
        self._cleanup()

        if not self.password:
            session = self._create_session(client_id, "none", None)
            return {
                "type": "auth_ok",
                "session": {
                    "id": session.id,
                    "auth": "none",
                },
            }

        server_nonce = random_hex()
        salt = random_hex()
        self._challenges[server_nonce] = PendingChallenge(
            client_id=client_id,
            server_nonce=server_nonce,
            salt=salt,
            created_at=time.time(),
        )
        return {
            "type": "auth_challenge",
            "challenge": {
                "server_nonce": server_nonce,
                "salt": salt,
                "iterations": PBKDF2_ITERATIONS,
                "algorithm": "PBKDF2-HMAC-SHA256",
            },
        }

    def complete_auth(self, message: dict[str, Any]) -> dict[str, Any]:
        if not self.password:
            raise AuthError("auth_not_required", "Password authentication is not enabled")

        self._cleanup()

        client_id = _required_str(message, "client_id")
        server_nonce = _required_str(message, "server_nonce")
        client_nonce = _required_str(message, "client_nonce")
        proof = _required_str(message, "proof")

        challenge = self._challenges.get(server_nonce)
        if not challenge:
            raise AuthError("challenge_expired", "Authentication challenge is missing or expired")
        if challenge.client_id != client_id:
            raise AuthError("client_mismatch", "Authentication client does not match the challenge")

        key = derive_password_key(self.password, challenge.salt)
        expected_proof = hmac_hex(key, proof_message(client_id, server_nonce, client_nonce))
        if not hmac.compare_digest(expected_proof, proof):
            raise AuthError("bad_password", "Password proof is invalid")

        session_key = hmac.new(
            key,
            session_message(client_id, server_nonce, client_nonce).encode("utf-8"),
            hashlib.sha256,
        ).digest()
        session = self._create_session(client_id, "hmac", session_key)
        self._challenges.pop(server_nonce, None)
        return {
            "type": "auth_ok",
            "session": {
                "id": session.id,
                "auth": "hmac",
            },
        }

    def verify_command(self, message: dict[str, Any]) -> Session:
        auth = message.get("auth")
        if not isinstance(auth, dict):
            raise AuthError("auth_required", "Command auth field is required")

        session_id = auth.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise AuthError("auth_required", "Session id is required")

        self._cleanup()

        session = self._sessions.get(session_id)
        if not session:
            raise AuthError("invalid_session", "Session is invalid or expired")

        if session.mode == "none":
            session.last_seen = time.time()
            return session

        counter = auth.get("counter")
        signature = auth.get("signature")
        if not isinstance(counter, int):
            raise AuthError("bad_signature", "Signed commands require an integer counter")
        if counter <= session.last_counter:
            raise AuthError("replay", "Command counter must increase")
        if not isinstance(signature, str) or not signature:
            raise AuthError("bad_signature", "Signed commands require a signature")
        if session.session_key is None:
            raise AuthError("bad_signature", "Session key is missing")
        if not verify_message_signature(message, session.session_key, signature):
            raise AuthError("bad_signature", "Command signature is invalid")

        session.last_counter = counter
        session.last_seen = time.time()
        return session

    def _create_session(self, client_id: str, mode: str, session_key: bytes | None) -> Session:
        session = Session(
            id=random_hex(),
            client_id=client_id,
            mode=mode,
            session_key=session_key,
            created_at=time.time(),
            last_seen=time.time(),
        )
        self._sessions[session.id] = session
        return session

    def _cleanup(self) -> None:
        now = time.time()
        expired_challenges = [
            nonce
            for nonce, challenge in self._challenges.items()
            if now - challenge.created_at > CHALLENGE_TTL_SECONDS
        ]
        for nonce in expired_challenges:
            self._challenges.pop(nonce, None)

        expired_sessions = [
            session_id
            for session_id, session in self._sessions.items()
            if now - session.last_seen > SESSION_TTL_SECONDS
        ]
        for session_id in expired_sessions:
            self._sessions.pop(session_id, None)


def _client_id(client: dict[str, Any]) -> str:
    value = client.get("id")
    if isinstance(value, str) and value:
        return value
    return "anonymous"


def _required_str(message: dict[str, Any], key: str) -> str:
    value = message.get(key)
    if not isinstance(value, str) or not value:
        raise AuthError("bad_auth_request", f"{key} is required")
    return value
