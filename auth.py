"""사용자 인증 — pbkdf2-sha256 해싱, JSON 저장 (stdlib만 사용)"""
import hashlib
import json
import secrets
from pathlib import Path
from typing import Optional


def hash_password(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}${h.hex()}"


def verify_password(stored: str, password: str) -> bool:
    try:
        salt, hash_hex = stored.split("$")
    except ValueError:
        return False
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return secrets.compare_digest(h.hex(), hash_hex)


class AuthSystem:
    """사용자 등록/로그인 관리"""

    def __init__(self, path: str = "users.json"):
        self.path = Path(path)
        self.users: dict = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self.users = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.users = {}

    def save(self):
        self.path.write_text(
            json.dumps(self.users, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def has_users(self) -> bool:
        return len(self.users) > 0

    def create_user(self, username: str, password: str) -> bool:
        username = username.strip()
        if not username or not password:
            return False
        if len(password) < 4:
            return False
        if username in self.users:
            return False
        self.users[username] = {"password": hash_password(password)}
        self.save()
        return True

    def verify(self, username: str, password: str) -> bool:
        u = self.users.get(username.strip())
        if not u:
            return False
        return verify_password(u["password"], password)
