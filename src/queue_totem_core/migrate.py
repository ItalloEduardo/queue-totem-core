"""CLI de migrations do pacote — chamada no deploy, nunca no boot.

    python -m queue_totem_core.migrate upgrade head --url postgresql+asyncpg://...

Roda ao lado do `alembic upgrade head` do host, numa cadeia separada: a tabela
de versões é `queue_versions_table` e o host continua dono de `alembic_version`.

A URL pode vir de `--url` ou das variáveis `QUEUE_TOTEM_DATABASE_URL` /
`DATABASE_URL`. Drivers sync e async são aceitos.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from alembic import command
from alembic.config import Config

VERSION_TABLE = "queue_versions_table"
SCRIPT_LOCATION = Path(__file__).resolve().parent / "migrations"


def build_config(url: str | None = None) -> Config:
    """Config do Alembic do pacote, sem depender de nenhum alembic.ini."""
    cfg = Config()
    cfg.set_main_option("script_location", str(SCRIPT_LOCATION))
    resolved = url or os.getenv("QUEUE_TOTEM_DATABASE_URL") or os.getenv("DATABASE_URL")
    if resolved:
        # Vai por attributes para escapar da interpolação de % do ConfigParser —
        # senhas com '%' quebrariam set_main_option.
        cfg.attributes["url"] = resolved
    return cfg


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m queue_totem_core.migrate",
        description="Migrations do queue-totem-core (tabela de versões: %s)"
        % VERSION_TABLE,
    )
    parser.add_argument(
        "--url",
        default=None,
        help="URL SQLAlchemy do banco; default: QUEUE_TOTEM_DATABASE_URL ou DATABASE_URL",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    upgrade = sub.add_parser("upgrade", help="aplica migrations")
    upgrade.add_argument("revision", nargs="?", default="head")

    downgrade = sub.add_parser("downgrade", help="reverte migrations")
    downgrade.add_argument("revision")

    stamp = sub.add_parser("stamp", help="marca a revisão sem executar nada")
    stamp.add_argument("revision", nargs="?", default="head")

    sub.add_parser("current", help="revisão aplicada no banco")
    sub.add_parser("heads", help="revisões mais recentes do pacote")
    sub.add_parser("history", help="histórico de revisões")

    args = parser.parse_args(argv)
    cfg = build_config(args.url)

    if args.action == "upgrade":
        command.upgrade(cfg, args.revision)
    elif args.action == "downgrade":
        command.downgrade(cfg, args.revision)
    elif args.action == "stamp":
        command.stamp(cfg, args.revision)
    elif args.action == "current":
        command.current(cfg, verbose=True)
    elif args.action == "heads":
        command.heads(cfg, verbose=True)
    elif args.action == "history":
        command.history(cfg, verbose=True)


if __name__ == "__main__":
    main()
