"""
Local password accounts, server-side sessions and the admin CLI (ADR 0011).

Why this shape: `SessionAuthenticator` is the only thing the service knows about sessions (the `Authenticator`
protocol), so P8 SSO can add another login class that calls the same `start_session`. `PasswordLogin` implements the
fixed login order of ADR 0011: read and shape-check the body, reserve the attempt atomically BEFORE any hashing,
look the account up, take a hashing slot, run exactly ONE scrypt (against `DUMMY_HASH` when there is no usable
account), and only then refund and create the session. Unknown user, wrong password, disabled account and bad
input all end in the same `LoginFailed` after the same hashing work.

Secrets: passwords, tokens (and their hashes) and the submitted username are never logged or put into an error
message. Only `sha256(token)` reaches the database. Settings are read from `config` at CALL time.

Admin CLI: `python -m src.accounts create|set-password|disable|enable|list`. Passwords are typed twice with
`getpass`, or piped with `--password-stdin`; never from argv or the environment. Run it as the service user so the
0600 state file keeps its owner.
"""

import argparse
import getpass as _getpass_module
import hashlib
import json
import math
import re
import secrets
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any, TextIO

from src import config, passwords, state
from src.interfaces import Authenticator
from src.models import Principal, SessionInfo
from src.observability import event
from src.passwords import DUMMY_HASH, KdfBusy, PasswordPolicyError

__all__ = [
    "AccountError",
    "LoginFailed",
    "LoginRateLimited",
    "PasswordLogin",
    "SessionAuthenticator",
    "create_account",
    "list_accounts",
    "main",
    "set_disabled",
    "set_password",
]

_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,512}")  # what secrets.token_urlsafe produces; anything else is not ours
_GLOBAL_KEY = "global"


class LoginFailed(Exception):
    """Wrong username or password (also: disabled account, malformed request). Deliberately one message."""

    def __init__(self) -> None:
        super().__init__("Wrong username or password.")


class LoginRateLimited(Exception):
    """Too many sign-in attempts, or the hashing slots are all busy. `retry_after` is in seconds."""

    def __init__(self, retry_after: float) -> None:
        super().__init__("Too many sign-in attempts. Please try again later.")
        self.retry_after = float(retry_after)


class AccountError(ValueError):
    """An admin action failed; the message is a fixed string that never echoes the input."""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------------------------ sessions
class SessionAuthenticator:
    """`Authenticator` backed by the `sessions` table: 256-bit random tokens, only their sha256 is stored."""

    def resolve(self, token: str) -> Principal | None:
        """The principal of a live session, or None for anything unknown, malformed, expired or revoked."""
        if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
            return None
        token_hash, now = _digest(token), time.time()
        found = state.get_session_principal(token_hash, now, config.SESSION_IDLE_MINUTES * 60)
        if found is None:
            return None
        if now - float(found["last_seen"]) >= state.TOUCH_INTERVAL_SECONDS:  # fresh sessions cost no write at all
            state.touch_session(token_hash, now)
        return {"account_id": str(found["account_id"]), "username": str(found["username"])}

    def start_session(
        self, account_id: str, username: str | None = None, *, expected_hash: str | None = None
    ) -> SessionInfo:
        """
        Issue a NEW session token for an account (never adopts one from the client). Raises LoginFailed when the
        account is disabled or gone, or (with `expected_hash`, set by password logins) when its password hash is no
        longer that one; both are re-checked inside the same transaction as the insert.
        """
        if username is None:
            username = next((a["username"] for a in state.list_accounts() if a["account_id"] == account_id), None)
            if username is None:
                raise LoginFailed
        token = secrets.token_urlsafe(32)
        now = time.time()
        expires = now + config.SESSION_MAX_HOURS * 3600
        if not state.create_session(
            _digest(token), account_id, now, expires, config.MAX_SESSIONS_PER_ACCOUNT, expected_hash
        ):
            raise LoginFailed
        return {"token": token, "username": str(username), "expires_at": expires}

    def logout(self, token: str) -> None:
        """Delete the session; unknown or malformed tokens are fine (logging out twice is harmless)."""
        if isinstance(token, str) and _TOKEN.fullmatch(token) is not None:
            state.delete_session(_digest(token))


# ------------------------------------------------------------------------------------------------ login
def _parse_body(raw_body: bytes) -> tuple[str, str] | None:
    """(canonical username, password) from the raw request body, or None for ANY bad shape. Never raises."""
    if not isinstance(raw_body, bytes | bytearray) or len(raw_body) > config.LOGIN_BODY_MAX_BYTES:
        return None
    try:
        data = json.loads(bytes(raw_body))
    except (ValueError, RecursionError):  # invalid JSON / UTF-8, or absurdly nested input
        return None
    username = data.get("username") if isinstance(data, dict) else None
    password = data.get("password") if isinstance(data, dict) else None
    if not isinstance(username, str) or not isinstance(password, str) or len(password) > config.PASSWORD_MAX_CHARS:
        return None
    canonical = passwords.canonical_username(username)
    try:
        password.encode("utf-8")  # lone surrogates from JSON escapes cannot be hashed
    except UnicodeEncodeError:
        return None
    if canonical is None:
        return None
    return canonical, password


def _settle_rehash(account: dict[str, Any], stored: str, upgraded: str, password: str) -> str | None:
    """Compare-and-set the rehash; return the hash to bind the session to, or None to bind to `stored`.

    Lost the race: if another login upgraded the same password, accept its hash (one more scrypt, inside the slot
    the caller holds). An admin reset will not verify, so None is returned and the login is refused downstream.
    """
    if state.rehash_password(str(account["account_id"]), stored, upgraded):
        return upgraded
    fresh = state.get_account_by_username(str(account["username"]))
    fresh_hash = str(fresh["password_hash"]) if fresh is not None else stored
    if (
        fresh_hash != stored
        and not passwords.needs_rehash(fresh_hash)
        and passwords.verify_password(password, fresh_hash)
    ):
        return fresh_hash
    return None


class PasswordLogin:
    """Verifies a username and password (ADR 0011 login order) and starts a session through the authenticator."""

    def __init__(self, authenticator: Authenticator | None = None) -> None:
        self.authenticator: Authenticator = authenticator if authenticator is not None else SessionAuthenticator()

    def login(self, raw_body: bytes, client_key: str | None) -> SessionInfo:
        """Raises LoginFailed (401) or LoginRateLimited (429). `client_key` None = address unknown: no client bucket.

        A storage outage anywhere on this path is the same 429 whatever the credentials (SPEC-GAP-082): a different
        answer would tell an attacker which usernames exist or which password was right.
        """
        try:
            return self._login(raw_body, client_key)
        except state.StorageUnavailable:
            event("auth.storage_unavailable")
            raise LoginRateLimited(1.0) from None

    def _login(self, raw_body: bytes, client_key: str | None) -> SessionInfo:
        # 1. Shape. A bad shape still costs a reservation (client + global keys only) and one dummy hash.
        parsed = _parse_body(raw_body)
        keys: list[str] = []
        limits: dict[str, tuple[int, float]] = {}
        window = config.LOGIN_WINDOW_MINUTES * 60.0
        user_key = client_hash = None
        if parsed is not None:
            user_key = _digest("user:" + parsed[0])
            keys.append(user_key)
            limits[user_key] = (config.LOGIN_MAX_FAILURES_PER_USER, window)
        if client_key is not None:
            client_hash = _digest("client:" + client_key)
            keys.append(client_hash)
            limits[client_hash] = (config.LOGIN_MAX_FAILURES_PER_CLIENT, window)
        keys.append(_GLOBAL_KEY)
        limits[_GLOBAL_KEY] = (config.LOGIN_MAX_PER_SECOND, 1.0)
        refundable = [k for k in (user_key, client_hash) if k is not None]

        # 2. Reserve atomically, before any hashing.
        now = time.time()
        attempt, retry_after = state.reserve_login_attempt(keys, limits, now)
        if attempt is None:
            event("auth.rate_limited")
            raise LoginRateLimited(max(1.0, math.ceil(retry_after)))
        state.prune_expired(now, config.SESSION_IDLE_MINUTES * 60, window)

        # 3. Account lookup (nothing to look up for a bad shape).
        account = state.get_account_by_username(parsed[0]) if parsed is not None else None
        stored = str(account["password_hash"]) if account is not None else DUMMY_HASH
        password = parsed[1] if parsed is not None else ""

        # 4 + 5. One hashing slot, exactly one scrypt.
        upgraded: str | None = None
        reverified: str | None = None
        try:
            with passwords.kdf_slot(config.KDF_WAIT_SECONDS):
                matched = passwords.verify_password(password, stored)
                if matched and account is not None and not account["disabled"] and passwords.needs_rehash(stored):
                    upgraded = passwords.hash_password(password)  # same slot: the rehash scrypt is bounded too
                    reverified = _settle_rehash(account, stored, upgraded, password)
        except KdfBusy:
            state.refund_login_attempt(attempt, keys)
            event("auth.busy")
            raise LoginRateLimited(1.0) from None

        # 6. Success needs the right password for a real, enabled account; everything else is the same failure.
        if account is None or not matched or account["disabled"]:
            event("auth.login_failed")
            raise LoginFailed
        account_id, username = str(account["account_id"]), str(account["username"])
        # Compare-and-set rehash; the session is then bound to whichever hash is current, so a password reset that
        # lands at any point before the insert refuses the login (SPEC-GAP-060: several atomic transactions).
        current = reverified if reverified is not None else stored
        try:
            info = self.authenticator.start_session(account_id, username, expected_hash=current)
        except LoginFailed:  # disabled between the check and the insert
            event("auth.login_failed")
            raise
        _refund_after_session(attempt, refundable)
        event("auth.login_succeeded", account_id=account_id)
        return info


def _refund_after_session(attempt: str, keys: list[str]) -> None:
    try:
        state.refund_login_attempt(attempt, keys)
    except state.StorageUnavailable:
        event("auth.refund_failed")  # the session exists; failing the login now would hide a live session


# ------------------------------------------------------------------------------------------------ admin
def _canonical(username: str) -> str:
    canonical = passwords.canonical_username(username)
    if canonical is None:
        raise AccountError("The username is not valid: 3-32 characters of a-z, 0-9, dot, dash or underscore.")
    return canonical


def _checked_hash(canonical: str, password: str) -> str:
    try:
        passwords.check_password_policy(password, canonical)
    except PasswordPolicyError as error:
        raise AccountError(str(error)) from None
    return passwords.hash_password(password)


def create_account(username: str, password: str) -> Principal:
    """Create an enabled account (admin only). The username is stored in canonical form."""
    canonical = _canonical(username)
    password_hash = _checked_hash(canonical, password)
    account_id = uuid.uuid4().hex
    try:
        state.create_account(account_id, canonical, password_hash)
    except state.DuplicateAccount:
        raise AccountError("That account already exists.") from None
    return {"account_id": account_id, "username": canonical}


def _existing(username: str) -> dict[str, Any]:
    account = state.get_account_by_username(_canonical(username))
    if account is None:
        raise AccountError("No such account.")
    return account


def set_password(username: str, password: str) -> None:
    """Change a password. Revokes every session of the account."""
    account = _existing(username)
    state.set_password_hash(str(account["account_id"]), _checked_hash(str(account["username"]), password))


def set_disabled(username: str, disabled: bool) -> None:
    """Disable (revokes sessions, keeps workspaces) or enable an account."""
    state.set_disabled(str(_existing(username)["account_id"]), disabled)


def list_accounts() -> list[dict[str, Any]]:
    """Accounts without their password hashes."""
    return state.list_accounts()


# ------------------------------------------------------------------------------------------------ CLI
def _read_password(from_stdin: bool, getpass: Callable[[str], str], stdin: TextIO) -> str:
    if from_stdin:
        line = stdin.readline()
        if not line:
            raise AccountError("No password was provided on standard input.")
        return line.rstrip("\r\n")
    if not stdin.isatty():
        raise AccountError("Standard input is not a terminal. Use --password-stdin to pipe the password.")
    first = getpass("Password: ")
    if getpass("Repeat password: ") != first:
        raise AccountError("The passwords did not match.")
    return first


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.accounts", description="TrustRAG account administration.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("create", "set-password"):
        sub = commands.add_parser(name, help=f"{name} for a username (password is prompted, never an argument)")
        sub.add_argument("username")
        sub.add_argument("--password-stdin", action="store_true", help="read one line from standard input")
    for name in ("disable", "enable"):
        commands.add_parser(name, help=f"{name} an account").add_argument("username")
    commands.add_parser("list", help="list accounts (no password hashes)")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    getpass: Callable[[str], str] = _getpass_module.getpass,
    stdin: TextIO = sys.stdin,
) -> int:
    """Run one admin command. Returns 0 on success, 1 on any failure (message on stderr, no input echoed)."""
    args = _parser().parse_args(argv)  # argparse itself exits with status 2 on usage errors
    try:
        if args.command in ("create", "set-password"):
            password = _read_password(args.password_stdin, getpass, stdin)
            if args.command == "create":
                create_account(args.username, password)
                print("Account created.")
            else:
                set_password(args.username, password)
                print("Password changed; all sessions of the account were signed out.")
        elif args.command in ("disable", "enable"):
            set_disabled(args.username, args.command == "disable")
            print(
                "Account disabled; its sessions were signed out." if args.command == "disable" else "Account enabled."
            )
        else:
            for account in list_accounts():
                status = "disabled" if account["disabled"] else "enabled"
                stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime(float(account["created"])))
                print(f"{account['username']}\t{status}\tcreated {stamp} UTC")
    except AccountError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except state.StorageUnavailable:
        print(
            f"Error: {config.STORAGE_UNAVAILABLE_MESSAGE}", file=sys.stderr
        )  # fixed text: no driver message, no traceback
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
