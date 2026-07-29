from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import asdict

from . import campaigns as campaign_core
from .adapters import AndroidAdapter, ScenarioStep
from .adb import AdbClient
from .analyzer import compare, stats_from_aggregates
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
    baseline_show.add_argument("--scenario", help="omit for the plain/no-scenario baseline")

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

    campaign = commands.add_parser(
        "campaign", help="long-running soak / repeat test programmes"
    )
    campaign_commands = campaign.add_subparsers(dest="campaign_command", required=True)

    campaign_start = campaign_commands.add_parser("start", help="create and run a campaign")
    campaign_start.add_argument("--serial", required=True)
    campaign_start.add_argument("--kind", required=True, choices=campaign_core.KINDS)
    campaign_start.add_argument("--duration", type=float, default=60,
                                help="seconds per run (for a soak, the length of the single run)")
    campaign_target = campaign_start.add_mutually_exclusive_group()
    campaign_target.add_argument("--scenario", metavar="NAME")
    campaign_target.add_argument("--tier", choices=youtube_scenarios.TIERS)
    campaign_start.add_argument("--iterations", type=int, default=1,
                                help="repeat campaigns only; a soak is always one run")
    campaign_start.add_argument("--create-only", action="store_true",
                                help="persist the campaign and its runs without executing them")

    campaign_resume = campaign_commands.add_parser(
        "resume", help="execute a campaign's remaining runs, skipping finished ones"
    )
    campaign_resume.add_argument("campaign_id")

    campaign_list = campaign_commands.add_parser("list")
    campaign_list.add_argument("--serial", dest="device_serial")
    campaign_list.add_argument("--limit", type=int, default=50)

    campaign_show = campaign_commands.add_parser("show")
    campaign_show.add_argument("campaign_id")
    campaign_show.add_argument("--threshold", type=float,
                               default=campaign_core.DEFAULT_REGRESSION_THRESHOLD_PCT)
    campaign_show.add_argument("--runs", action="store_true",
                               help="include every child run row in the output")

    campaign_cancel = campaign_commands.add_parser("cancel")
    campaign_cancel.add_argument("campaign_id")
    return root


def _progress_reporter(total: int):
    """Print each finished run to stderr as a campaign proceeds.

    stderr, not stdout, so the JSON result stays pipeable -- a campaign can
    run for hours and its progress is worth watching, but that must not
    corrupt the machine-readable output the other commands all produce.
    """
    state = {"done": 0}

    def report(run: dict | None) -> None:
        state["done"] += 1
        if run is None:
            return
        scenario = run["youtube_scenario"] or "(no scenario)"
        print(f"[{state['done']}/{total}] {scenario}: {run['status']}", file=sys.stderr)

    return report


def _campaign_command(args, storage: Storage, adb: AdbClient) -> int:
    if args.campaign_command == "start":
        spec = campaign_core.CampaignSpec(
            kind=args.kind, serial=args.serial, duration=args.duration,
            scenario=args.scenario, tier=args.tier, iterations=args.iterations,
        )
        try:
            created = campaign_core.create_campaign(storage, spec)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.create_only:
            print(json.dumps(created, indent=2))
            return 0
        print(f"campaign {created['campaign_id']}: {created['count']} run(s)", file=sys.stderr)
        result = campaign_core.execute_campaign(
            storage, adb, created["campaign_id"],
            on_run=_progress_reporter(created["count"]),
        )
        print(json.dumps({**created, **result}, indent=2))
        return 0

    if args.campaign_command == "resume":
        campaign = storage.get_campaign(args.campaign_id)
        if campaign is None:
            print("Campaign not found", file=sys.stderr)
            return 1
        remaining = [r for r in storage.list_campaign_runs(args.campaign_id)
                     if r["status"] not in campaign_core.TERMINAL_RUN_STATUSES]
        print(f"resuming {len(remaining)} remaining run(s)", file=sys.stderr)
        result = campaign_core.execute_campaign(
            storage, adb, args.campaign_id, on_run=_progress_reporter(len(remaining))
        )
        print(json.dumps(result, indent=2))
        return 0

    if args.campaign_command == "list":
        print(json.dumps(
            campaign_core.campaign_summaries(storage, limit=args.limit,
                                             device_serial=args.device_serial),
            indent=2,
        ))
        return 0

    if args.campaign_command == "cancel":
        try:
            print(json.dumps(campaign_core.cancel_campaign(storage, args.campaign_id), indent=2))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    try:
        detail = campaign_core.campaign_detail(storage, args.campaign_id, args.threshold)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not args.runs:
        detail.pop("runs", None)
    print(json.dumps(detail, indent=2))
    return 0


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
        run_id = args.resume
        if args.youtube_scenario:
            adapter = AndroidAdapter()
            screen = adapter.screen_size(adb, args.serial)
            scenario = youtube_scenarios.build(args.youtube_scenario, screen)
            if run_id is None:
                # Pre-create the row with the scenario name recorded --
                # TestRunner.run() sees an existing row for this run_id and
                # skips its own create_run(), preserving the field.
                run_id = uuid.uuid4().hex
                storage.create_run(run_id, args.serial, youtube_scenario=args.youtube_scenario)
        elif args.app:
            adapter = AndroidAdapter()
            scenario = [ScenarioStep(0.0, "launch_app", {"package": args.app})]
        run_id = TestRunner(storage, adb, default_collectors(), adapter=adapter, scenario=scenario).run(
            args.serial, args.duration, run_id
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
            print(json.dumps(storage.get_baseline(args.serial, run["youtube_scenario"]), indent=2))
        else:
            baseline = storage.get_baseline(args.serial, args.scenario)
            if baseline is None:
                scenario_desc = f"scenario {args.scenario!r}" if args.scenario else "plain runs"
                print(f"No baseline set for this device ({scenario_desc})")
                return 1
            stats = stats_from_aggregates(storage.aggregate_samples(baseline["run_id"]))
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
        baseline = storage.get_baseline(run["device_serial"], run["youtube_scenario"])
        if baseline is None:
            scenario_desc = f"scenario {run['youtube_scenario']!r}" if run["youtube_scenario"] else "plain runs"
            print(f"No baseline set for device {run['device_serial']} ({scenario_desc})")
            return 1
        baseline_stats = stats_from_aggregates(storage.aggregate_samples(baseline["run_id"]))
        candidate_stats = stats_from_aggregates(storage.aggregate_samples(args.run_id))
        results = compare(baseline_stats, candidate_stats, threshold_pct=args.threshold)
        print(json.dumps({
            "baseline_run_id": baseline["run_id"],
            "candidate_run_id": args.run_id,
            "regressed": any(r.regressed for r in results),
            "metrics": [asdict(r) for r in results],
        }, indent=2))
    elif args.command == "youtube-scenarios":
        print(json.dumps(youtube_scenarios.describe_scenarios(tier=args.tier), indent=2))
    elif args.command == "campaign":
        return _campaign_command(args, storage, adb)
    else:
        adapter = AndroidAdapter()
        screen = adapter.screen_size(adb, args.serial)
        results = []
        for name in youtube_scenarios.list_scenarios(tier=args.tier):
            scenario = youtube_scenarios.build(name, screen)
            run_id = uuid.uuid4().hex
            storage.create_run(run_id, args.serial, youtube_scenario=name)
            TestRunner(storage, adb, default_collectors(), adapter=adapter, scenario=scenario).run(
                args.serial, args.duration, run_id
            )
            results.append({"scenario": name, "run_id": run_id, "status": storage.get_run(run_id)["status"]})
        print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
