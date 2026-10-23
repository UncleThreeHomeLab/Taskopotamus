import os

import psycopg

from src.runtime import read_secret


class Database:
    def __init__(self, **settings):
        self.settings = settings

    @classmethod
    def from_env(cls):
        return cls(
            host=os.environ.get("DATABASE_HOST", "postgres"),
            port=os.environ.get("DATABASE_PORT", "5432"),
            dbname=os.environ["DATABASE_NAME"], user=os.environ["DATABASE_USER"],
            password=read_secret("DATABASE_PASSWORD"),
            sslmode=os.environ.get("DATABASE_SSLMODE", "prefer"),
        )

    def connect(self):
        return psycopg.connect(
            **self.settings, connect_timeout=5,
            options="-c statement_timeout=10000 -c lock_timeout=5000 -c idle_in_transaction_session_timeout=240000",
        )
