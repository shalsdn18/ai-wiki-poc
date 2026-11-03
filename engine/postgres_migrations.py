"""CLI entry point for the opt-in PostgreSQL migration runner."""

from __future__ import annotations

from engine.postgres_operational import migrate


def main() -> None:
    applied = migrate()
    print(f"PostgreSQL migrations applied: {applied}")


if __name__ == "__main__":
    main()
