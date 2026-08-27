"""CLI entry point for the Singpass attendance flow.

By default the OTP is prompted in this terminal (``CliOtpProvider``). Use
``--http-otp`` to instead receive it via the HTTP bridge (``POST /otp``).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import replace

from .attendance import Answers, DayType, build_answers, choose_day_type
from .bot import run_flow
from .config import Config, load_config
from .otp import CliOtpProvider, OtpProvider
from .server import OtpBridge, create_app, make_server, serve_quietly, stop_server

log = logging.getLogger("mdcattendance")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mdcattendance",
        description="Singpass attendance form auto-fill bot with an OTP step.",
    )
    parser.add_argument(
        "--day-type",
        choices=[day_type.value for day_type in DayType],
        help="attendance profile; prompts when omitted (normal, wfh, mc)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--discover",
        action="store_true",
        help="save the authenticated form HTML; submit nothing",
    )
    mode.add_argument(
        "--preflight",
        action="store_true",
        help="verify selectors up to the credential form; do not log in or ask for OTP",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="authenticate and fill the selected profile without submitting",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="show the browser window (default: headless)",
    )
    parser.add_argument(
        "--http-otp",
        action="store_true",
        help="receive the OTP via the HTTP bridge (POST /otp) instead of a terminal prompt",
    )
    parser.add_argument(
        "--otp-timeout",
        type=float,
        default=None,
        help="seconds to wait for the OTP (HTTP bridge only; ignored for the terminal prompt)",
    )
    return parser.parse_args()




async def amain() -> None:
    _setup_logging()
    args = _parse_args()
    cfg: Config = load_config()

    if args.discover:
        cfg = replace(cfg, discover=True)
    if args.preflight:
        cfg = replace(cfg, preflight=True)
    if args.dry_run:
        cfg = replace(cfg, dry_run=True)
    if args.headed:
        cfg = replace(cfg, headless=False)
    if args.otp_timeout is not None:
        cfg = replace(cfg, otp_timeout=args.otp_timeout)

    if not cfg.preflight and (not cfg.singpass_id or not cfg.singpass_password):
        raise SystemExit(
            "SINGPASS_ID and SINGPASS_PASSWORD must be set in .env (see .env.example). "
            "Use --preflight to verify selectors without credentials."
        )

    answers: Answers = {}
    if not cfg.preflight and not cfg.discover:
        day_type = await choose_day_type(args.day_type)
        log.info("attendance day type: %s", day_type.value)
        answers = await build_answers(day_type)

    otp_provider: OtpProvider
    server = None
    server_task = None

    if args.http_otp and not cfg.preflight:
        bridge = OtpBridge()
        app = create_app(bridge)
        server = make_server(app, cfg.otp_host, cfg.otp_port)
        server_task = asyncio.create_task(serve_quietly(server))
        await asyncio.sleep(0.8)
        if not server.started:
            raise SystemExit(
                f"OTP bridge could not bind {cfg.otp_host}:{cfg.otp_port} "
                "(port in use?); set OTP_PORT in .env"
            )
        log.info(
            "OTP bridge ready: POST http://%s:%s/otp  (GET /health)",
            cfg.otp_host,
            cfg.otp_port,
        )
        otp_provider = bridge
    else:
        otp_provider = CliOtpProvider()
        if not cfg.preflight:
            log.info("OTP will be prompted in this terminal when the 2FA step is reached")

    try:
        await run_flow(cfg, otp_provider, answers)
    finally:
        if server is not None and server_task is not None:
            await stop_server(server, server_task)


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
