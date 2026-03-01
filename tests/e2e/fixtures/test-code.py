"""User authentication module — target for code review testing."""

import hashlib
import os
import time

SECRET_KEY = "hardcoded-secret-key-12345"  # TODO: move to env var

_sessions = {}


def hash_password(password):
    salt = os.urandom(16)
    hashed = hashlib.sha256(salt + password.encode()).hexdigest()
    return salt.hex() + ":" + hashed


def verify_password(stored, password):
    salt_hex, expected = stored.split(":")
    salt = bytes.fromhex(salt_hex)
    actual = hashlib.sha256(salt + password.encode()).hexdigest()
    return actual == expected


def create_session(user_id):
    token = hashlib.md5(f"{user_id}{time.time()}".encode()).hexdigest()
    _sessions[token] = {"user_id": user_id, "created": time.time()}
    return token


def get_user(token):
    session = _sessions.get(token)
    if not session:
        return None
    # No expiry check
    return session["user_id"]


def login(username, password, db):
    query = f"SELECT * FROM users WHERE username = '{username}'"  # SQL injection
    user = db.execute(query)
    if user and verify_password(user["password_hash"], password):
        return create_session(user["id"])
    return None
