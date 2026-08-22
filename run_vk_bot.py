"""VK transport entry point. Mock mode never performs network operations."""

import logging
import random
import signal
import time

from database import connect
from vk.adapter import VKAdapter
from vk.client import MockVKClient, RealVKClient
from vk.config import VKConfig
from vk.exceptions import TemporaryVKError


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = VKConfig.from_env()
    if config.mode == "mock":
        VKAdapter(lambda: connect(config.db_path), MockVKClient(), config)
        logging.info("VK adapter initialized in mock mode; network is disabled")
        return

    client = RealVKClient(config.token or "", config.api_version)
    adapter = VKAdapter(lambda: connect(config.db_path), client, config)
    stopping = False

    def stop(*_args) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    logging.info("VK Group Long Poll started")
    failures = 0
    while not stopping:
        try:
            for event in client.long_poll_events(config.group_id or 0):
                adapter.handle_event(event)
                if stopping:
                    break
            failures = 0
        except TemporaryVKError:
            failures += 1
            delay = min(30.0, 0.5 * (2 ** min(failures - 1, 6))) + random.uniform(0, 0.25)
            logging.exception("VK Long Poll temporary failure; restart=%s delay=%.2f", failures, delay)
            time.sleep(delay)


if __name__ == "__main__":
    main()
