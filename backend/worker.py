#!/usr/bin/env python3
"""
Sill Workers

This module contains the maintenance worker (subconscious substrate upkeep):
   - Runs `run_subconscious_maintenance()` on its own schedule (`should_run_maintenance()`)
   - Optionally bridges outbox/inbox to RabbitMQ (integration plumbing; the bridge
     itself lives in `rabbit_bridge.py` — see that module's docstring for why)

Reflective processing ships as the beat worker (see `docs/beats.md`), which is off
until you turn it on.
"""

import asyncio
import json
import logging
import os
from typing import Any

import asyncpg
from dotenv import load_dotenv
import argparse

from rabbit_bridge import RabbitBridge, RABBITMQ_ENABLED


# Load environment
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('maintenance_worker')

# Database configuration
DB_CONFIG = {
    'host': os.getenv('POSTGRES_HOST', 'localhost'),
    'port': int(os.getenv('POSTGRES_PORT', 5432)),
    'database': os.getenv('POSTGRES_DB', 'sill'),
    'user': os.getenv('POSTGRES_USER', 'sill'),
    'password': os.getenv('POSTGRES_PASSWORD', 'sill_password'),
}

# Worker configuration
POLL_INTERVAL = float(os.getenv('WORKER_POLL_INTERVAL', 1.0))  # seconds


class MaintenanceWorker:
    """Subconscious maintenance loop: consolidates/prunes substrate on its own trigger."""

    def __init__(self):
        self.pool: asyncpg.Pool | None = None
        self.running = False
        self._rabbit_bridge: RabbitBridge | None = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(**DB_CONFIG, min_size=1, max_size=5)
        logger.info(f"Connected to database at {DB_CONFIG['host']}:{DB_CONFIG['port']}")
        self._rabbit_bridge = RabbitBridge(self.pool)
        if RABBITMQ_ENABLED:
            await self.ensure_rabbitmq_ready()

    async def disconnect(self):
        if self.pool:
            await self.pool.close()
            logger.info("Disconnected from database")

    async def should_run(self) -> bool:
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval("SELECT should_run_maintenance()"))

    async def run_maintenance_tick(self) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            raw = await conn.fetchval("SELECT run_subconscious_maintenance('{}'::jsonb)")
            if isinstance(raw, str):
                return json.loads(raw)
            return dict(raw) if isinstance(raw, dict) else {"result": raw}

    async def run_if_due(self) -> None:
        if await self.should_run():
            stats = await self.run_maintenance_tick()
            logger.info(f"Subconscious maintenance: {stats}")

    # RabbitMQ (optional outbox/inbox bridge; uses management HTTP API).
    # Delegates to the extracted RabbitBridge (see rabbit_bridge.py) — a single
    # bridge instance is created in connect() and lives for the worker's
    # lifetime, so _last_rabbit_inbox_poll throttling is real persisted state
    # rather than something copied in and out of a throwaway instance.
    async def ensure_rabbitmq_ready(self) -> None:
        await self._rabbit_bridge.ensure_rabbitmq_ready()

    async def publish_outbox_messages(self, max_messages: int = 20) -> int:
        return await self._rabbit_bridge.publish_outbox_messages(max_messages=max_messages)

    async def poll_inbox_messages(self, max_messages: int = 10) -> int:
        return await self._rabbit_bridge.poll_inbox_messages(max_messages=max_messages)

    async def run(self):
        self.running = True
        logger.info("Maintenance worker starting...")
        await self.connect()
        try:
            while self.running:
                try:
                    if RABBITMQ_ENABLED:
                        await self.poll_inbox_messages()
                        await self.publish_outbox_messages(max_messages=10)
                    await self.run_if_due()
                except Exception as e:
                    logger.error(f"Maintenance loop error: {e}")
                await asyncio.sleep(POLL_INTERVAL)
        finally:
            await self.disconnect()

    def stop(self):
        self.running = False
        logger.info("Maintenance worker stopping...")


async def _amain(mode: str) -> None:
    """Async entry point for workers."""
    maint_worker = MaintenanceWorker()

    import signal

    def shutdown(signum, frame):
        maint_worker.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    mode = (mode or "maintenance").strip().lower()
    if mode == "maintenance":
        await maint_worker.run()
        return
    raise ValueError("mode must be one of: maintenance")


def main() -> int:
    """Console-script entry point."""
    p = argparse.ArgumentParser(prog="sill-worker", description="Run Sill background workers.")
    p.add_argument(
        "--mode",
        choices=["maintenance", "beat"],
        default=os.getenv("SILL_WORKER_MODE", "maintenance"),
        help="Which worker to run.",
    )
    p.add_argument("--once", action="store_true", help="Run one beat attempt and exit.")
    args = p.parse_args()
    if args.once and args.mode != "beat":
        p.error("--once requires --mode beat")
    if args.mode == "beat":
        from beat_worker import main as beat_main

        return beat_main(once=args.once)

    asyncio.run(_amain(args.mode))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
