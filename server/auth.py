import base64
import hashlib
import hmac
import os
import re
from typing import Tuple


MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
USERNAME_PATTERN = re.compile(r"^[A-Za-zА-Яа-яЁё0-9_.-]{3,32}$")
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_KEY_LENGTH = 64
SCRYPT_MAX_MEMORY = 64 * 1024 * 1024


class CredentialsError(ValueError):
    pass


def validate_username(username: object) -> Tuple[str, str]:
    if not isinstance(username, str):
        raise CredentialsError("Имя пользователя должно быть строкой.")

    display_name = username.strip()
    if not USERNAME_PATTERN.fullmatch(display_name):
        raise CredentialsError(
            "Имя должно содержать 3–32 буквы, цифры или символы _ . -."
        )

    return display_name.casefold(), display_name


def validate_password(password: object) -> str:
    if not isinstance(password, str):
        raise CredentialsError("Пароль должен быть строкой.")
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise CredentialsError(
            f"Пароль должен содержать от {MIN_PASSWORD_LENGTH} "
            f"до {MAX_PASSWORD_LENGTH} символов."
        )
    return password


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    password_hash = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_KEY_LENGTH,
        maxmem=SCRYPT_MAX_MEMORY,
    )
    return "$".join(
        (
            "scrypt",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(password_hash).decode("ascii"),
        )
    )


def verify_password(password: str, encoded_hash: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected_hash = encoded_hash.split("$", 5)
        if (
            algorithm != "scrypt"
            or int(n) != SCRYPT_N
            or int(r) != SCRYPT_R
            or int(p) != SCRYPT_P
        ):
            return False

        actual_hash = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt, validate=True),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=SCRYPT_KEY_LENGTH,
            maxmem=SCRYPT_MAX_MEMORY,
        )
        return hmac.compare_digest(
            actual_hash, base64.b64decode(expected_hash, validate=True)
        )
    except (ValueError, TypeError):
        return False


DUMMY_PASSWORD_HASH = hash_password("password-used-only-for-timing-protection")
