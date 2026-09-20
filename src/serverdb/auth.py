from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

from .errors import Forbidden, Unauthorized

VALID_ROLES = {"analyst", "reviewer", "admin"}
_PBKDF2_ROUNDS = 120_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ROUNDS).hex()
    return f"pbkdf2${_PBKDF2_ROUNDS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, rounds, salt, digest = stored.split("$")
        if scheme != "pbkdf2":
            return False
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return hmac.compare_digest(check, digest)
    except (ValueError, TypeError):
        return False


class Auth:
    """令牌为 base64url(username).hmac_sha256(secret, username)。

    不引入 JWT 依赖：令牌不透明、可通过更换服务密钥整体失效。
    """

    def __init__(self, secret: str | None = None) -> None:
        self.secret = (secret or os.getenv("SERVERDB_SECRET") or "dev-secret-change-me").encode()

    def issue(self, username: str) -> str:
        payload = base64.urlsafe_b64encode(username.encode()).decode().rstrip("=")
        sig = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{sig}"

    def username_of(self, token: str) -> str:
        try:
            payload, sig = token.split(".", 1)
        except ValueError as exc:
            raise Unauthorized("令牌格式错误") from exc
        expected = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            raise Unauthorized("令牌签名无效")
        pad = "=" * (-len(payload) % 4)
        try:
            return base64.urlsafe_b64decode(payload + pad).decode()
        except Exception as exc:
            raise Unauthorized("令牌载荷无效") from exc


def require_user(user: dict | None) -> dict:
    if user is None:
        raise Unauthorized("缺少有效身份令牌")
    return user


def require_role(user: dict, *roles: str) -> dict:
    require_user(user)
    if user["role"] not in roles:
        raise Forbidden(f"需要角色: {', '.join(roles)}")
    return user
