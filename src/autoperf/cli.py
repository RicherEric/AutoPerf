from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from .adapters import AndroidAdapter, ScenarioStep
from .adb import AdbClient
from .analyzer import compare, compute_stats
from .collectors import default_collectors
from .runner import TestRunner
from .scenarios import youtube as youtube_scenarios
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
    driver = run.add_mutually_exclusive_group()
    driver.add_argument("--app", metavar="PACKAGE")
    driver.add_argument("--youtube-scenario", metavar="NAME", dest="youtube_scenario")
    many = commands.add_parser("run-many")
    many.add_argument("--serial", action="append", required=True, dest="serials")
    many.add_argument("--duration", type=float, default=60)
    status = commands.add_parser("status")
    status.add_argument("run_id")

    baseline = commands.add_parser("baseline")
    baseline_commands = baseline.add_subparsers(dest="baseline_command", required=True)
    baseline_set = baseline_commands.add_parser("set")
    baseline_set.add_argument("--serial", required=True)
    baseline_set.add_argument("--run", required=True, dest="run_id")
    baseline_show = baseline_commands.add_parser("show")
    baseline_show.add_argument("--serial", required=True)

    compare_cmd = commands.add_parser("compare")
    compare_cmd.add_argument("--run", required=True, dest="run_id")
    compare_cmd.add_argument("--threshold", type=float, default=20.0)

    yt = commands.add_parser("youtube-scenarios")
    yt_commands = yt.add_subparsers(dest="youtube_scenarios_command", required=True)
    yt_list = yt_commands.add_parser("list")
    yt_list.add_argument("--tier", choices=youtube_scenarios.TIERS)

    suite = commands.add_parser("run-suite")
    suite.add_argument("--serial", required=True)
    suite.add_argument("--tier", required=True, choices=youtube_scenarios.TIERS)
    suite.add_argument("--duration", type=float, default=30)
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
        adapter = None
        scenario = None
        if args.youtube_scenario:
            adapter = AndroidAdapter()
            screen = adapter.screen_size(adb, args.serial)
            scenario = youtube_scenarios.build(args.youtube_scenario, screen)
        elif args.app:
            adapter = AndroidAdapter()
            scenario = [ScenarioStep(0.0, "launch_app", {"package": args.app})]
        run_id = TestRunner(storage, adb, default_collectors(), adapter=adapter, scenario=scenario).run(
            args.serial, args.duration, args.resume
        )
        print(run_id)
    elif args.command == "run-many":
        results = DeviceSupervisor(storage).run_many(args.serials, args.duration)
        print(json.dumps([{"run_id": r.run_id, "serial": r.serial, "exit_code": r.exit_code}
                          for r in results], indent=2))
    elif args.command == "baseline":
        if args.baseline_command == "set":
            run = storage.get_run(args.run_id)
            if run is None:
                print("Run not found")
                return 1
            if run["device_serial"] != args.serial:
                print(f"Run {args.run_id} belongs to device {run['device_serial']}, not {args.serial}")
                return 1
            storage.set_baseline(args.serial, args.run_id)
            print(json.dumps(storage.get_baseline(args.serial), indent=2))
        else:
            baseline = storage.get_baseline(args.serial)
            if baseline is None:
                print("No baseline set for this device")
                return 1
            stats = compute_stats(storage.list_samples(baseline["run_id"], limit=100_000))
            print(json.dumps({
                "run_id": baseline["run_id"],
                "created_at": baseline["created_at"],
                "stats": {name: asdict(value) for name, value in stats.items()},
            }, indent=2))
    elif args.command == "compare":
        run = storage.get_run(args.run_id)
        if run is None:
            print("Run not found")
            return 1
        baseline = storage.get_baseline(run["device_serial"])
        if baseline is None:
            print(f"No baseline set for device {run['device_serial']}")
            return 1
        baseline_stats = compute_stats(storage.list_samples(baseline["run_id"], limit=100_000))
        candidate_stats = compute_stats(storage.list_samples(args.run_id, limit=100_000))
        results = compare(baseline_stats, candidate_stats, threshold_pct=args.threshold)
        print(json.dumps({
            "baseline_run_id": baseline["run_id"],
            "candidate_run_id": args.run_id,
            "regressed": any(r.regressed for r in results),
            "metrics": [asdict(r) for r in results],
        }, indent=2))
    elif args.command == "youtube-scenarios":
        print(json.dumps(youtube_scenarios.describe_scenarios(tier=args.tier), indent=2))
    else:
        adapter = AndroidAdapter()
        screen = adapter.screen_size(adb, args.serial)
        results = []
        for name in youtube_scenarios.list_scenarios(tier=args.tier):
            scenario = youtube_scenarios.build(name, screen)
            run_id = TestRunner(storage, adb, default_collectors(), adapter=adapter, scenario=scenario).run(
                args.serial, args.duration
            )
            results.append({"scenario": name, "run_id": run_id, "status": storage.get_run(run_id)["status"]})
        print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
