from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import asdict

from . import campaigns as campaign_core
from .adapters import ScenarioStep
from .adb import AdbClient
from .profiles import select_profile
from .analyzer import app_version_delta, compare, stats_from_aggregates
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
    campaign_start.add_argument("--preflight", action="store_true",
                                help="verify the UI selectors on this device first, and abort if any need updating")

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

    pre = commands.add_parser(
        "preflight",
        help="check every UI selector against one device before running a real test",
    )
    pre.add_argument("--serial", required=True)
    scope = pre.add_mutually_exclusive_group()
    scope.add_argument("--scenario", action="append", dest="scenarios",
                       help="check only this scenario (repeatable)")
    scope.add_argument("--tier", choices=youtube_scenarios.TIERS)
    scope.add_argument("--all", action="store_true",
                       help="check every scenario instead of a minimal covering set")
    pre.add_argument("--allow-fallback", action="store_true",
                     help="treat a coordinate fallback as acceptable rather than as a finding")

    ui_dump = commands.add_parser(
        "ui-dump",
        help="print the device's on-screen elements, for capturing real selectors",
    )
    ui_dump.add_argument("--serial", required=True)
    ui_dump.add_argument("--limit", type=int, default=60)
    ui_dump.add_argument("--raw", action="store_true", help="print the raw hierarchy XML instead")

    cap = commands.add_parser(
        "capture",
        help="store what the device says about the current screen, as a test fixture",
    )
    cap.add_argument("--serial", required=True)
    cap.add_argument("--name", default=None,
                     help="what screen this is, e.g. home_feed or watch_page")
    cap.add_argument("--app", default=None,
                     help="package to pin the capture to, so it records which build it came from")
    cap.add_argument("--list", action="store_true", dest="list_only",
                     help="list stored captures and stop")
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


def _run_preflight(adb: AdbClient, serial: str, *, scenarios=None, allow_fallback=False) -> tuple[int, dict]:
    """Check the selectors on one device and print a work list.

    Returns (exit_code, summary). A coordinate fallback counts as a finding by
    default: the target still got tapped, but it was located the fragile way,
    which is precisely what this command exists to detect before an hour of
    measurement is spent on top of it.
    """
    from . import preflight as preflight_core
    from .profiles import select_profile

    profile = select_profile(adb, serial)
    adapter = profile.adapter()
    names = scenarios or preflight_core.covering_scenarios()
    print(f"preflight: {len(names)} scenario(s) covering every selector target", file=sys.stderr)

    def announce(name):
        print(f"  -> {name}", file=sys.stderr)

    def report(check):
        if check.status != preflight_core.MATCHED:
            print(f"     [{check.status}] {check.target}", file=sys.stderr)

    summary = preflight_core.run_preflight(
        adb, adapter, serial, scenarios=names, on_scenario=announce, on_check=report,
        profile=profile,
    )
    if allow_fallback:
        # Fallbacks stay in the report either way -- this only stops them
        # from failing the command.
        blocking = [entry for entry in summary["needs_attention"]
                    if entry["status"] != [preflight_core.FALLBACK]]
        summary["ok"] = not blocking and not summary["failed_verifications"]
    return (0 if summary["ok"] else 1), summary


def _campaign_command(args, storage: Storage, adb: AdbClient) -> int:
    if args.campaign_command == "start":
        spec = campaign_core.CampaignSpec(
            kind=args.kind, serial=args.serial, duration=args.duration,
            scenario=args.scenario, tier=args.tier, iterations=args.iterations,
        )
        if getattr(args, "preflight", False):
            try:
                validated = spec.validated()
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            # Scoped to the scenarios this campaign will actually run, not to
            # every scenario that exists: blocking a campaign over a selector
            # it is never going to touch would make the gate an obstacle
            # rather than a safeguard. Deduplicated, and `None` entries
            # (plain sampling runs) dropped since they drive no UI at all.
            planned = sorted({name for name in validated.planned_scenarios() if name})
            if not planned:
                print("preflight: campaign drives no UI, nothing to check", file=sys.stderr)
            else:
                # Runs before the campaign row exists: a campaign that aborts
                # partway leaves a half-finished record to clean up, and the
                # point of a preflight is to stop before any of it is created.
                code, summary = _run_preflight(adb, args.serial, scenarios=planned)
                if code:
                    print(json.dumps(summary, indent=2))
                    print("preflight failed -- campaign not started. Fix the selectors listed "
                          "above in scenarios/selectors.py, or re-run with --allow-fallback.",
                          file=sys.stderr)
                    return code
                print("preflight passed", file=sys.stderr)
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
        # One probe for the command, so the adapter and the collectors are the
        # same platform's. Asking for them separately is how a TV gets driven
        # correctly and then measured for a battery it does not have.
        profile = select_profile(adb, args.serial)
        adapter = None
        scenario = None
        run_id = args.resume
        if args.youtube_scenario:
            adapter = profile.adapter()
            screen = adapter.screen_size(adb, args.serial)
            scenario = youtube_scenarios.build(args.youtube_scenario, screen)
            if run_id is None:
                # Pre-create the row with the scenario name recorded --
                # TestRunner.run() sees an existing row for this run_id and
                # skips its own create_run(), preserving the field.
                run_id = uuid.uuid4().hex
                storage.create_run(run_id, args.serial, youtube_scenario=args.youtube_scenario)
        elif args.app:
            adapter = profile.adapter()
            scenario = [ScenarioStep(0.0, "launch_app", {"package": args.app})]
        run_id = TestRunner(storage, adb, profile.collectors(), adapter=adapter, scenario=scenario).run(
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
        versions = app_version_delta(storage.get_run(baseline["run_id"]), run)
        quality = storage.run_quality(args.run_id)
        if versions["changed"]:
            print("WARNING: the app under test changed between these two runs; "
                  "the delta below describes the app's change, not the device's.",
                  file=sys.stderr)
        if not quality["verified"]:
            print(f"WARNING: candidate run recorded {quality['verification_failures']} verification "
                  "failure(s) -- its metrics measure a screen the scenario never reached.",
                  file=sys.stderr)
        print(json.dumps({
            "baseline_run_id": baseline["run_id"],
            "candidate_run_id": args.run_id,
            "regressed": any(r.regressed for r in results),
            "app_version": versions,
            "candidate_quality": quality,
            "metrics": [asdict(r) for r in results],
        }, indent=2))
    elif args.command == "youtube-scenarios":
        print(json.dumps(youtube_scenarios.describe_scenarios(tier=args.tier), indent=2))
    elif args.command == "campaign":
        return _campaign_command(args, storage, adb)
    elif args.command == "preflight":
        if args.all:
            scenarios = youtube_scenarios.list_scenarios()
        elif args.tier:
            scenarios = youtube_scenarios.list_scenarios(tier=args.tier)
        else:
            scenarios = args.scenarios  # None -> minimal covering set
        code, summary = _run_preflight(
            adb, args.serial, scenarios=scenarios, allow_fallback=args.allow_fallback
        )
        print(json.dumps(summary, indent=2))
        if summary["needs_attention"]:
            print(f"\n{len(summary['needs_attention'])} target(s) need their selectors updated "
                  "in src/autoperf/scenarios/selectors.py:", file=sys.stderr)
            for entry in summary["needs_attention"]:
                print(f"  {entry['target']}: {entry['detail']}", file=sys.stderr)
        return code
    elif args.command == "capture":
        # A hand-written fixture is a guess about a device nobody had in front
        # of them, and the git log records what that costs: a parser that
        # matched no modern device, nineteen wrong selector labels, a search
        # flow that never typed. This is how a test gets evidence instead.
        from . import capture as capture_core

        if args.list_only:
            print(json.dumps(capture_core.list_captures(), indent=2, ensure_ascii=False))
            return 0
        if not args.name:
            print("error: --name is required (what screen is this?)", file=sys.stderr)
            return 2
        try:
            entry = capture_core.capture_screen(adb, args.serial, args.name, package=args.app)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(entry, indent=2, ensure_ascii=False))
        print(f"stored {len(entry['reads'])} read(s) for {entry['name']!r} "
              f"from {entry['device']['model']} / Android {entry['device']['android_release']}",
              file=sys.stderr)

    elif args.command == "ui-dump":
        # The resource-ids and labels in scenarios/selectors.py cannot be
        # verified without the app in front of you -- they are internal to
        # each app build and published nowhere. This is how the real ones get
        # captured: open the screen in question on the device, run this, and
        # paste what it prints into that table.
        from . import uiauto

        try:
            xml = uiauto.dump_hierarchy(adb, args.serial)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.raw:
            print(xml)
            return 0
        nodes = uiauto.parse_hierarchy(xml)
        print(json.dumps({
            "serial": args.serial,
            "focus": uiauto.current_focus(adb, args.serial),
            "node_count": len(nodes),
            "elements": uiauto.describe_clickables(nodes, limit=args.limit),
        }, indent=2))
    else:
        profile = select_profile(adb, args.serial)
        adapter = profile.adapter()
        screen = adapter.screen_size(adb, args.serial)
        results = []
        for name in youtube_scenarios.list_scenarios(tier=args.tier):
            scenario = youtube_scenarios.build(name, screen)
            run_id = uuid.uuid4().hex
            storage.create_run(run_id, args.serial, youtube_scenario=name)
            TestRunner(storage, adb, profile.collectors(), adapter=adapter, scenario=scenario).run(
                args.serial, args.duration, run_id
            )
            results.append({"scenario": name, "run_id": run_id, "status": storage.get_run(run_id)["status"]})
        print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
