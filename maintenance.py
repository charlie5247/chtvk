"""Small retention command for transport and interaction records."""

import argparse
from datetime import datetime, timedelta, timezone

from database import connect
from interaction_repository import InteractionRepository
from vk.deduplication import EventDeduplicator


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/bot.db")
    parser.add_argument("--messages-days", type=int, default=90)
    args = parser.parse_args()
    connection = connect(args.db)
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=args.messages_days)).isoformat(timespec="seconds")
        interactions = InteractionRepository(connection)
        messages = interactions.purge_messages_before(cutoff)
        unknown = interactions.purge_unknown_before(cutoff)
        clarifications = interactions.purge_expired_clarifications()
        events = EventDeduplicator(connection, 1).cleanup()
        print(f"Удалено messages: {messages}; unknown_questions: {unknown}; pending_clarifications: {clarifications}; processed_vk_events: {events}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
