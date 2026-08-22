#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import configparser
import logging
from pathlib import Path
import sys

from meshcore import MeshCore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.service_plugins.repeater_monitor_service import (
    RepeaterMonitorService,
    RepeaterTarget,
)


class _DBManagerStub:
    def __init__(self, db_path: str):
        self.db_path = db_path


class _BotStub:
    def __init__(self, root: Path, config: configparser.ConfigParser, meshcore: MeshCore, db_path: str):
        self.bot_root = root
        self.config = config
        self.meshcore = meshcore
        self.db_manager = _DBManagerStub(db_path)
        self.logger = logging.getLogger("single_repeater_poll")
        self.connected = True


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("node_key")
    parser.add_argument("display_name")
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--db-path", default=str(PROJECT_ROOT / "meshcore_bot.db"))
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config.ini"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    config = configparser.ConfigParser()
    config.read(args.config)

    mc = await MeshCore.create_serial(args.port, baudrate=args.baud, debug=False, only_error=False)
    if mc is None:
        print("failed: could not connect to companion")
        return 2

    try:
        await mc.ensure_contacts()
        bot = _BotStub(PROJECT_ROOT, config, mc, args.db_path)
        service = RepeaterMonitorService(bot)
        target = RepeaterTarget(node_key=args.node_key, display_name=args.display_name)
        await service._poll_target(target, force=True)
        print("single poll complete")
    finally:
        await mc.disconnect()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
