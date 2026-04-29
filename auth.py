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
        """레거시: 성공 여부만 반환. 사유가 필요하면 create_user_detail 사용."""
        return self.create_user_detail(username, password) is None

    def create_user_detail(self, username: str, password: str) -> Optional[str]:
        """성공이면 None, 실패면 사유 메시지(한국어)를 반환."""
        username = username.strip()
        if not username:
            return "사용자명을 입력해주세요."
        if not password:
            return "비밀번호를 입력해주세요."
        if len(password) < 4:
            return "비밀번호는 4자 이상이어야 합니다."
        if username in self.users:
            return "이미 존재하는 사용자명입니다."
        self.users[username] = {"password": hash_password(password)}
        self.save()
        return None

    def verify(self, username: str, password: str) -> bool:
        u = self.users.get(username.strip())
        if not u:
            return False
        return verify_password(u["password"], password)
