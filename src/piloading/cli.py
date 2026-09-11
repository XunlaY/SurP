"""CLI entry point: `piloading run` and `piloading auth-viessmann`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable

import uvicorn

from piloading.clients.base import BatterySystemClient, WallboxClient
from piloading.clients.goe import GoeWallboxClient
from piloading.clients.mock import MockBatterySystemClient, MockClock, MockWallboxClient
from piloading.clients.viessmann import ViessmannClient
from piloading.clients.viessmann_auth import run_auth_setup
from piloading.config import Settings
from piloading.controller import LatestState, run_control_loop
from piloading.history import HistoryStore
from piloading.logging_setup import configure_logging
from piloading.web.app import create_app

logger = logging.getLogger(__name__)


def _build_clients(
    settings: Settings,
) -> tuple[BatterySystemClient, WallboxClient, list[Callable[[], Awaitable[None]]]]:
    if settings.mode == "mock":
        clock = MockClock()
        wallbox = MockWallboxClient(clock, voltage=settings.voltage)
        battery = MockBatterySystemClient(clock, wallbox)
        return battery, wallbox, []

    if not settings.viessmann_auth_file.exists():
        raise SystemExit(
            f"Keine Viessmann-Zugangsdaten unter {settings.viessmann_auth_file} gefunden. "
            "Bitte zuerst `piloading auth-viessmann` ausführen."
        )
    goe = GoeWallboxClient(settings.goe_host)
    viessmann = ViessmannClient(settings.viessmann_auth_file, invert_grid_power_sign=settings.invert_grid_power_sign)
    return viessmann, goe, [goe.aclose, viessmann.aclose]


async def _run_async(settings: Settings) -> None:
    battery_client, wallbox_client, closers = _build_clients(settings)
    history = HistoryStore(settings.db_path)
    state = LatestState()

    app = create_app(state, history, env_file=settings.env_file)
    server = uvicorn.Server(
        uvicorn.Config(app, host=settings.web_host, port=settings.web_port, log_level="warning")
    )

    logger.info(
        "piLoading startet: mode=%s dry_run=%s dashboard=http://%s:%d",
        settings.mode,
        settings.dry_run,
        settings.web_host,
        settings.web_port,
    )

    control_task = asyncio.create_task(run_control_loop(settings, battery_client, wallbox_client, history, state))
    server_task = asyncio.create_task(server.serve())

    # SIGTERM (systemd stop/restart) otherwise kills the process immediately, skipping the
    # cleanup below — route it through the same graceful-shutdown path as KeyboardInterrupt.
    stop_event = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop_event.set)
    stop_task = asyncio.create_task(stop_event.wait())

    try:
        done, pending = await asyncio.wait(
            {control_task, server_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            task.result()  # re-raise if this task ended due to an exception
        if stop_task in done:
            logger.info("SIGTERM empfangen, fahre piLoading sauber herunter ...")
    finally:
        control_task.cancel()
        server_task.cancel()
        stop_task.cancel()
        await asyncio.gather(control_task, server_task, stop_task, return_exceptions=True)
        for closer in closers:
            await closer()
        history.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="piloading")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="Regelschleife + Web-Dashboard starten")
    subparsers.add_parser("auth-viessmann", help="Einmalige Viessmann-OAuth-Anmeldung durchführen")
    args = parser.parse_args()

    configure_logging()
    settings = Settings()

    if args.command == "run":
        try:
            asyncio.run(_run_async(settings))
        except KeyboardInterrupt:
            pass
    elif args.command == "auth-viessmann":
        run_auth_setup(settings)


if __name__ == "__main__":
    main()
