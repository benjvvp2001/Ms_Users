"""Apply users migrations explicitly, independently from API startup."""

from pathlib import Path

import psycopg

from app.core.config import get_database_url
from app.database.session import normalize_dsn


MIGRATIONS = (
    "002_sportmach_users_integration.sql",
    "003_password_reset.sql",
    "004_email_verification.sql",
    "005_profile_goals_zone.sql",
    "006_reports_blocks.sql",
    "007_chat_mutes.sql",
    "008_current_location.sql",
    "009_reporte_rol.sql",
    "010_audit_admin.sql",
    "011_reporte_escalado.sql",
    "012_sanciones.sql",
    "013_audit_fecha_timestamptz.sql",
    "014_reporte_revision_autor.sql",
    "015_foto_perfil.sql",
)


def migration_names(already_created: bool) -> list[str]:
    """The files to apply, in order: 001 only on a base without the schema, then every later one (all idempotent)."""
    return list(MIGRATIONS) if already_created else ["001_users_schema.sql", *MIGRATIONS]


def main() -> None:
    directory = Path(__file__).resolve().parents[2] / "db" / "migrations"
    with psycopg.connect(normalize_dsn(get_database_url()), autocommit=True) as conn:
        existing = conn.execute("SELECT to_regclass('public.usuario')").fetchone()[0]
        migrations = migration_names(existing is not None)
        for name in migrations:
            conn.execute((directory / name).read_text(encoding="utf-8"))
            print(f"Applied {name}")


if __name__ == "__main__":
    main()
