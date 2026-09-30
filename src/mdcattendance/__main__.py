"""Terminal attendance interface; every browser mode uses the shared runner."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from dataclasses import replace

from .attendance import (
    Answers,
    DayType,
    build_answers,
    choose_day_type,
    validate_answers,
)
from .config import load_config
from .otp import CliOtpProvider, OtpProvider, validate_otp_token
from .runner import AttendanceRunner, BusyRun, DuplicateRun
from .server import OtpHttpServer
from .storage import StateStore


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mdcattendance", description="Bounded Singpass attendance submission."
    )
    parser.add_argument("--day-type", choices=[kind.value for kind in DayType])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--discover", action="store_true", help="save restricted form diagnostics; do not submit"
    )
    mode.add_argument(
        "--preflight", action="store_true", help="check login selectors without credentials"
    )
    mode.add_argument(
        "--dry-run", action="store_true", help="authenticate, fill and verify without submitting"
    )
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--otp-timeout", type=float, help="maximum seconds for OTP delivery")
    parser.add_argument(
        "--http-otp",
        action="store_true",
        help="receive OTP only by authenticated phone POST; profile/MC prompts remain terminal input",
    )
    parser.add_argument("--state-dir", help="shared persistent state directory")
    parser.add_argument(
        "--override",
        action="store_true",
        help="intentionally permit a duplicate submission; check prior outcomes first",
    )
    return parser.parse_args()


async def amain(args: argparse.Namespace) -> int:
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    received_signal: list[int] = []

    def stop(signum: int) -> None:
        received_signal.append(signum)
        if task is not None:
            task.cancel()

    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop, signum)
    store = None
    runner = None
    receiver = None
    try:
        cfg = load_config()
        changes = {}
        if args.discover or args.preflight or args.dry_run:
            changes.update(discover=args.discover, preflight=args.preflight, dry_run=args.dry_run)
        if args.headed:
            changes["headless"] = False
        if args.otp_timeout is not None:
            changes["otp_timeout"] = args.otp_timeout
        if args.state_dir is not None:
            changes["state_dir"] = args.state_dir
        if getattr(args, "http_otp", False):
            changes["otp_http_enabled"] = True
        cfg = replace(cfg, **changes)
        if not cfg.preflight and (not cfg.singpass_id or not cfg.singpass_password):
            print(
                "Credentials are required for this mode; provision a restricted local env file. "
                "Use --preflight without credentials.",
                file=sys.stderr,
            )
            return 2
        if args.override and (cfg.preflight or cfg.discover or cfg.dry_run):
            print("--override is only valid for an intentional submission.", file=sys.stderr)
            return 2
        otp_provider: OtpProvider = CliOtpProvider()
        if cfg.otp_http_enabled and not cfg.preflight:
            validate_otp_token(cfg.otp_http_token)
            receiver = OtpHttpServer(lambda: {"cli": cfg.otp_http_token}, cfg.otp_http_port)
            await receiver.start()
            otp_provider = receiver.provider(
                "cli", None if getattr(args, "http_otp", False) else otp_provider
            )
            print("OTP receiver ready; phone delivery uses authenticated /otp/pending and /otp POST.")
        answers: Answers = {}
        if not cfg.preflight and not cfg.discover:
            async with asyncio.timeout(cfg.prompt_timeout):
                day_type = await choose_day_type(args.day_type, timeout=cfg.prompt_timeout)
                answers = await build_answers(day_type, timeout=cfg.prompt_timeout)
                validate_answers(answers)
        store = StateStore(cfg.state_dir, cfg.retention_days)
        runner = AttendanceRunner(cfg, store)
        result = await runner.run(cfg, otp_provider, answers, override=args.override)
        if result.status == "unknown":
            print(
                "UNKNOWN: submission may have reached FormSG. Check the form outcome manually; "
                "do not retry automatically.",
                file=sys.stderr,
            )
            return 1
        if result.status == "confirmed":
            print("Attendance submission confirmed.")
            return 0
        expected = (
            "preflight"
            if cfg.preflight
            else "discovered"
            if cfg.discover
            else "dry_run"
            if cfg.dry_run
            else "confirmed"
        )
        if result.status == expected:
            print(
                {
                    "preflight": "Preflight completed; no login or submission performed.",
                    "discovered": "Discovery completed; no submission performed.",
                    "dry_run": "Dry-run completed; no submission performed.",
                }[result.status]
            )
            return 0
        print(f"Run outcome: {result.status}. No confirmation was obtained.", file=sys.stderr)
        return 1
    except BusyRun:
        print("Another attendance run is active; this request was not started.", file=sys.stderr)
        return 1
    except DuplicateRun:
        print(
            "Duplicate submission blocked. Check the prior outcome; use --override only for an "
            "intentional resubmission.",
            file=sys.stderr,
        )
        return 1
    except asyncio.CancelledError:
        print(
            "Run cancelled; consult the persistent attempt journal before resubmitting.", file=sys.stderr
        )
        return 128 + (received_signal[0] if received_signal else signal.SIGINT)
    except TimeoutError:
        print("Input or run timed out; no successful submission is claimed.", file=sys.stderr)
        return 1
    except (ValueError, OSError, EOFError):
        print(
            "Configuration or terminal input is invalid/unavailable; check local settings and "
            "secret-file permissions.",
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            "Run failed; no successful submission is claimed. Consult the persistent attempt journal.",
            file=sys.stderr,
        )
        return 1
    finally:
        # A second signal must not interrupt browser cleanup or journal updates.
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)
            signal.signal(signum, signal.SIG_IGN)
        try:
            if runner is not None:
                await runner.shutdown()
        finally:
            try:
                if receiver is not None:
                    await receiver.stop()
            finally:
                if store is not None:
                    store.close()
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(
                    signum, signal.default_int_handler if signum == signal.SIGINT else signal.SIG_DFL
                )


def main() -> None:
    args = _parse_args()  # --help must never load credentials or open state.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        raise SystemExit(asyncio.run(amain(args)))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
