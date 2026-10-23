import re
import time

import jwt


class ProfileTokens:
    def __init__(self, secret):
        if not isinstance(secret, str) or len(secret) < 32 or not secret.isascii():
            raise ValueError("Profile signing key must contain at least 32 ASCII characters.")
        self.secret = secret

    def issue(self, chat_id):
        self.validate_chat(chat_id)
        now = int(time.time())
        return jwt.encode(
            {"sub": chat_id, "iat": now, "exp": now + 300, "aud": "taskopotamus-profiles"},
            self.secret, algorithm="HS256",
        )

    @staticmethod
    def validate_chat(chat_id):
        if not isinstance(chat_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", chat_id):
            raise ValueError("A numeric private chat ID is required.")

    def verify(self, authorization):
        try:
            if not isinstance(authorization, str) or not authorization.startswith("Bearer ") or len(authorization) > 2048:
                raise ValueError()
            claims = jwt.decode(
                authorization[7:], self.secret, algorithms=["HS256"], audience="taskopotamus-profiles",
                options={"require": ["sub", "iat", "exp", "aud"]},
            )
            self.validate_chat(claims["sub"])
            if claims["exp"] - claims["iat"] != 300:
                raise ValueError()
            return claims["sub"]
        except (jwt.PyJWTError, ValueError, TypeError):
            raise PermissionError("Invalid profile authorization.") from None
