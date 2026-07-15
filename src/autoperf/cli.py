from __future__ import annotations

import argparse
import json

from .adapters import AndroidAdapter, ScenarioStep
from .adb import AdbClient
from .collectors import default_collectors
from .runner import TestRunner
from .storage import Storage
from .workers import DeviceSupervisor


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="autoperf")
    root.add_argument("--db", default="autoperf.db")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("devices")
    run = commands.add_parser("run")
    run.add_argument("--serial", required=True)
    run.add_argument("--duration", type=float, default=60)
    run.add_argument("--resume", metavar="RUN_ID")
    run.add_argument("--app", metavar="PACKAGE")
    many = commands.add_parser("run-many")
    many.add_argument("--serial", action="append", required=True, dest="serials")
    many.add_argument("--duration", type=float, default=60)
    status = commands.add_parser("status")
    status.add_argument("run_id")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    storage = Storage(args.db)
    storage.initialize()
    adb = AdbClient()
    if args.command == "devices":
        devices = adb.devices()
        for device in devices:
            storage.register_device(device)
        print(json.dumps([{"serial": d.serial, "state": d.state, "model": d.model} for d in devices], indent=2))
    elif args.command == "status":
        result = storage.get_run(args.run_id)
        if not result:
            print("Run not found")
            return 1
        print(json.dumps(result, indent=2))
    elif args.command == "run":
        adapter = AndroidAdapter() if args.app else None
        scenario = [ScenarioStep(0.0, "launch_app", {"package": args.app})] if args.app else None
        run_id = TestRunner(storage, adb, default_collectors(), adapter=adapter, scenario=scenario).run(
            args.serial, args.duration, args.resume
        )
        print(run_id)
    else:
        results = DeviceSupervisor(storage).run_many(args.serials, args.duration)
        print(json.dumps([{"run_id": r.run_id, "serial": r.serial, "exit_code": r.exit_code}
                          for r in results], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
