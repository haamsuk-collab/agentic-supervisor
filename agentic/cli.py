"""Command line: python3.12 -m agentic new|run|report|verify ..."""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from agentic import admin, cards, checks, config, daily, health, inbox, ledger, loop, notify, outbox, report
from agentic.config import Config, valid_id
from agentic.i18n import t
from agentic.ledger import Ledger, LedgerError
from agentic.mission import MissionError, validate_request

EXIT_OK, EXIT_NOT_COMPLETE, EXIT_ERROR = 0, 1, 2


def _resolve_mission(arg: str, cfg: Config) -> str:
    """`arg` is a mission id, or a mission file which is installed into the state folder first."""
    if valid_id(arg) and not os.path.isfile(arg):
        return arg
    return admin.install_mission_file(cfg, arg)


def _require_id(mission_id: str) -> str:
    if not valid_id(mission_id):
        raise MissionError(f"{mission_id!r} is not a valid mission id")
    return mission_id


def _cmd_run(args: argparse.Namespace, cfg: Config) -> int:
    mission_id = _resolve_mission(args.mission, cfg)
    status = loop.run(mission_id, cfg)
    print(t("cli.run.done", id=mission_id, status=status, report=cfg.report_file(mission_id)))
    return EXIT_OK if status == "complete" else EXIT_NOT_COMPLETE


def _read_request_file(path: str) -> bytes:
    """The request text as bytes: UTF-8, not empty, at most REQUEST_MAX_BYTES, no NUL characters."""
    return validate_request(Path(path).read_bytes())


def _cmd_new(args: argparse.Namespace, cfg: Config) -> int:
    mission_id = _require_id(args.id or datetime.now(timezone.utc).strftime("m-%Y%m%d-%H%M%S"))
    admin.create_request(cfg, mission_id, _read_request_file(args.request_file))
    return _cmd_run(argparse.Namespace(mission=mission_id), cfg)


def _cmd_report(args: argparse.Namespace, cfg: Config) -> int:
    mission_id = _require_id(args.mission)
    with loop.mission_lock(cfg, mission_id):  # a running supervisor writes the report itself
        records = Ledger(cfg.ledger_file(mission_id)).read()  # verifies first
        print(report.write_report(cfg, mission_id, records))
    return EXIT_OK


def _cmd_verify(args: argparse.Namespace, cfg: Config) -> int:
    if getattr(args, "anchors", None) is not None:
        if args.mission is not None:
            raise ValueError("cannot specify both a mission id and --anchors")
        anchors = ledger.parse_anchors(Path(args.anchors).read_text(encoding="utf-8"))
        if not anchors:
            raise ValueError("no ledger anchors found in file")
        all_ok = True
        for mission_id, seq, prefix in anchors:
            outcome, detail = ledger.verify_anchor(cfg.ledger_file(mission_id), seq, prefix)
            if outcome == "ok":
                print(t("cli.verify.anchor_ok", id=mission_id, seq=seq, hash=prefix))
            elif outcome == "missing":
                print(t("cli.verify.anchor_missing", id=mission_id, seq=seq, hash=prefix))
                all_ok = False
            else:
                print(t("cli.verify.anchor_mismatch", id=mission_id, seq=seq, hash=prefix, detail=detail or ""))
                all_ok = False
        return EXIT_OK if all_ok else EXIT_ERROR
    if args.mission is None:
        raise ValueError("mission id or --anchors <file> is required")
    path = cfg.ledger_file(_require_id(args.mission))
    result = Ledger(path).verify()
    if path.exists() and ledger.append_only(path) is not True:  # also when the flag cannot be read
        print(t("cli.verify.no_append_only"), file=sys.stderr)
    if result.ok:
        print(t("cli.verify.ok", count=result.count))
        if result.bound is False:
            print(t("cli.verify.unbound"))
        line = _folder_line(cfg, args.mission, Ledger(path).read())
        if line is not None:
            print(line)
        return EXIT_OK
    print(t("cli.verify.broken", seq=result.bad_seq, reason=result.reason), file=sys.stderr)
    return EXIT_ERROR


VERIFY_MAX_PATHS = 10


def _folder_line(cfg: Config, mission_id: str, records: list[dict]) -> str | None:
    """The worker folder against the final check snapshot of a finished, bound mission: unchanged, changed (up to
    VERIFY_MAX_PATHS paths, sorted) or missing. None when there is no final snapshot to compare with."""
    finished = next((r for r in reversed(records) if r["type"] == "mission_finished"), None)
    final = finished["data"].get("final_snapshot_sha256") if finished else None
    if not final:
        return None
    workdir = cfg.workdir(mission_id)
    if not workdir.is_dir():
        return t("cli.verify.folder_missing")
    current = checks.tree_manifest(workdir)
    if ledger.manifest_digest(current) == final:
        return t("cli.verify.folder_unchanged")
    recorded = finished["data"].get("final_manifest")
    if recorded is None:  # the run that finished could not record the manifest: the paths are unknown
        return t("cli.verify.folder_changed", count="?", paths="?")
    changed = sorted(rel for rel in current.keys() | recorded.keys() if current.get(rel) != recorded.get(rel))
    shown = ", ".join(changed[:VERIFY_MAX_PATHS]) + (", …" if len(changed) > VERIFY_MAX_PATHS else "")
    return t("cli.verify.folder_changed", count=len(changed), paths=shown)


def _cmd_migrate_workspaces(args: argparse.Namespace, cfg: Config) -> int:
    """Move every worker folder from the Stage B–E root to cfg.workspaces as the worker user (SPEC-F section 1)."""
    if os.geteuid() != 0:
        print(t("cli.migrate.need_root"), file=sys.stderr)
        return EXIT_ERROR
    for mission_id, outcome in admin.migrate_workspaces(cfg, Path(args.old_root)):
        print(t("cli.migrate.outcome", id=mission_id, outcome=outcome))
    return EXIT_OK


def _cmd_inbox(args: argparse.Namespace, cfg: Config) -> int:
    return inbox.process(cfg)


def _cmd_notify_failure(args: argparse.Namespace, cfg: Config) -> int:
    notify.notify_failure(cfg, _require_id(args.mission))
    return EXIT_OK


def _cmd_health(args: argparse.Namespace, cfg: Config) -> int:
    blocks = health.card(health.run(cfg))
    print("\n".join(cards.plain_text(b) for b in blocks))
    try:
        outbox.write_cards(cfg, blocks)
    except FileNotFoundError:
        pass
    return EXIT_OK


def _cmd_honesty_canary(args: argparse.Namespace, cfg: Config) -> int:
    daily.start_honesty_canary(cfg)
    return EXIT_OK


def _cmd_backup(args: argparse.Namespace, cfg: Config) -> int:
    status = daily.backup(status_file=cfg.state / daily.BACKUP_STATUS_FILE)
    return EXIT_OK if status["ok"] else EXIT_ERROR



def _cmd_config(args: argparse.Namespace, cfg: Config) -> int:
    print(config.format_settings(config.effective_settings(cfg)))
    return EXIT_OK

def _cmd_mission(args: argparse.Namespace, cfg: Config) -> int:
    """The operator's admin command: it starts and stops system services, so it is for root only."""
    if os.geteuid() != 0:
        print(t("cli.mission.need_root"), file=sys.stderr)
        return EXIT_ERROR
    action = args.action
    if action == "new":
        mission_id = admin.new_mission(cfg, _read_request_file(args.request_file).decode("utf-8"), args.id)
        print(t("cli.mission.started", id=mission_id))
    elif action == "start":
        print(t("cli.mission.started", id=admin.start_mission(cfg, args.file)))
    elif action == "list":
        for line in admin.list_missions(cfg):
            print(line)
    elif action == "status":
        print(admin.mission_status(cfg, args.mission))
        print()
        print(admin.report_text(cfg, args.mission), end="")
    elif action == "report":
        return _cmd_report(args, cfg)
    else:
        admin.stop_mission(cfg, args.mission)
        print(t("cli.mission.stopped", id=args.mission))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentic", description=t("cli.description"))
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new", help=t("cli.new.help"))
    new.add_argument("request_file")
    new.add_argument("--id", help=t("cli.id.help"))
    new.set_defaults(func=_cmd_new)
    for name, func, help_key in (
        ("run", _cmd_run, "cli.run.help"),
        ("report", _cmd_report, "cli.report.help"),
    ):
        p = sub.add_parser(name, help=t(help_key))
        p.add_argument("mission")
        p.set_defaults(func=func)
    verify_parser = sub.add_parser("verify", help=t("cli.verify.help"))
    verify_parser.add_argument("mission", nargs="?")
    verify_parser.add_argument("--anchors", help=t("cli.verify.anchors.help"))
    verify_parser.set_defaults(func=_cmd_verify)
    p = sub.add_parser("inbox", help=t("cli.inbox.help"))
    p.set_defaults(func=_cmd_inbox)
    p = sub.add_parser("notify-failure", help=t("cli.notify_failure.help"))
    p.add_argument("mission")
    p.set_defaults(func=_cmd_notify_failure)
    admin_parser = sub.add_parser("mission", help=t("cli.mission.help"))
    admin_parser.set_defaults(func=_cmd_mission)
    actions = admin_parser.add_subparsers(dest="action", required=True)
    p = actions.add_parser("new", help=t("cli.mission.new.help"))
    p.add_argument("request_file", metavar="REQUEST")
    p.add_argument("id", nargs="?", metavar="ID", help=t("cli.id.help"))
    p = actions.add_parser("start", help=t("cli.mission.start.help"))
    p.add_argument("file", metavar="FILE")
    actions.add_parser("list", help=t("cli.mission.list.help"))
    for name, help_key in (("status", "cli.mission.status.help"),
                           ("report", "cli.report.help"),
                           ("stop", "cli.mission.stop.help")):
        p = actions.add_parser(name, help=t(help_key))
        p.add_argument("mission", metavar="ID")
    for name, func, help_key in (
        ("health", _cmd_health, "cli.health.help"),
        ("honesty-canary", _cmd_honesty_canary, "cli.honesty_canary.help"),
        ("backup", _cmd_backup, "cli.backup.help"),
    ):
        p = sub.add_parser(name, help=t(help_key))
        p.set_defaults(func=func)
    p = sub.add_parser("migrate-workspaces", help=t("cli.migrate.help"))
    p.add_argument("old_root", nargs="?", default=config.OLD_WORKSPACES,
                   help=t("cli.migrate.old_root.help", default=config.OLD_WORKSPACES))
    p.set_defaults(func=_cmd_migrate_workspaces)
    cfg_parser = sub.add_parser("config", help=t("cli.config.help"))
    cfg_parser.set_defaults(func=_cmd_config)
    cfg_actions = cfg_parser.add_subparsers(dest="config_action", required=True)
    cfg_actions.add_parser("check", help=t("cli.config.check.help"))
    args = parser.parse_args(argv)
    if config.SETTINGS_ERROR is not None:
        print(t("cli.config.invalid", path=config.SETTINGS_PATH, error=config.SETTINGS_ERROR), file=sys.stderr)
        return EXIT_NOT_COMPLETE
    try:
        return args.func(args, config.load())
    except (MissionError, LedgerError, loop.MissionBusy, config.ConfigError, ValueError, OSError) as exc:
        print(t("cli.error", error=exc), file=sys.stderr)
        return EXIT_ERROR
