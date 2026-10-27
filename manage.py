#!/usr/bin/env python3
"""OpenBudget CLI. Run `python manage.py --help`."""
import argparse
import os

from openbudget.app import serve
from openbudget.db import connect, initialize
from openbudget.seed import DEMO_PASSWORD, seed


def main():
    parser = argparse.ArgumentParser(description="OpenBudget participatory budgeting platform")
    parser.add_argument("command", choices=["init", "seed", "serve"], help="Initialize, seed demo data, or serve")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    parser.add_argument("--db", default=None, help="Custom SQLite database path")
    args = parser.parse_args()
    with connect(args.db) as db:
        initialize(db)
        if args.command == "seed":
            added = seed(db)
            print("Demo data created." if added else "Database already contains budget cycles. Seed skipped.")
            if added:
                print(f"Citizen: mira@demo.local / {DEMO_PASSWORD}")
                print(f"Moderator: moderator@demo.local / {DEMO_PASSWORD}")
        elif args.command == "init":
            print("Database initialized. Use `python manage.py seed` for demo content.")
    if args.command == "serve":
        serve(args.host, args.port, args.db)


if __name__ == "__main__":
    main()
