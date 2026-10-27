"""Session, password and HTTP security helpers."""
import hashlib
import hmac
import os
import secrets
import time

SESSION_AGE = 7 * 24 * 3600


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 300_000)
    return f"pbkdf2_sha256$300000${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, n, salt, actual = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(n))
        return hmac.compare_digest(candidate, bytes.fromhex(actual))
    except (TypeError, ValueError):
        return False


def create_session(db, user_id=None):
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    csrf = secrets.token_urlsafe(32)
    db.execute("INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at) VALUES(?,?,?,?)",
               (token_hash, user_id, csrf, int(time.time()) + SESSION_AGE))
    return raw_token, csrf


def lookup_session(db, raw):
    if not raw:
        return None
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return db.execute("""SELECT s.*,u.name,u.email,u.role FROM sessions s
        LEFT JOIN users u ON s.user_id=u.id
        WHERE s.token_hash=? AND s.expires_at>?""", (digest, int(time.time()))).fetchone()


def revoke_session(db, raw):
    if raw:
        db.execute("DELETE FROM sessions WHERE token_hash=?",
                   (hashlib.sha256(raw.encode()).hexdigest(),))
