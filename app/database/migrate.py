"""Apply users migrations explicitly, independently from API startup."""

from pathlib import Path

import psycopg

from app.core.config import get_database_url
from app.database.session import normalize_dsn


def main() -> None:
    directory = Path(__file__).resolve().parents[2] / "db" / "migrations"
    with psycopg.connect(normalize_dsn(get_database_url()), autocommit=True) as conn:
        existing = conn.execute("SELECT to_regclass('public.usuario')").fetchone()[0]
        migrations = [] if existing else ["001_users_schema.sql"]
        migrations.append("002_sportmach_users_integration.sql")
        migrations.append("003_password_reset.sql")
        migrations.append("004_email_verification.sql")
        migrations.append("005_profile_goals_zone.sql")
        for name in migrations:
            conn.execute((directory / name).read_text(encoding="utf-8"))
            print(f"Applied {name}")


if __name__ == "__main__":
    main()
