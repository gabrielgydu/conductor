"""Conductor CLI entry point."""
import argparse
import sys


def _cmd_init(args):
    print("Not implemented yet")


def _cmd_plan(args):
    print("Not implemented yet")


def _cmd_run(args):
    print("Not implemented yet")


def _cmd_status(args):
    print("Not implemented yet")


def _cmd_log(args):
    print("Not implemented yet")


def _cmd_cleanup(args):
    print("Not implemented yet")


def main():
    parser = argparse.ArgumentParser(prog="conductor", description="Conductor orchestration tool")
    subparsers = parser.add_subparsers(dest="subcommand", metavar="subcommand")

    p_init = subparsers.add_parser("init", help="Initialize a new run")
    p_init.add_argument("--name", required=True)
    p_init.add_argument("--base-branch", default="main")
    p_init.add_argument("--preset", default="base")

    p_plan = subparsers.add_parser("plan", help="Generate a plan")
    p_plan.add_argument("--brief", action="store_true")

    p_run = subparsers.add_parser("run", help="Execute a run")
    p_run.add_argument("--overnight", action="store_true")

    subparsers.add_parser("status", help="Show run status")

    p_log = subparsers.add_parser("log", help="Show logs")
    p_log.add_argument("--tail", type=int, default=50)

    p_cleanup = subparsers.add_parser("cleanup", help="Clean up worktrees")
    p_cleanup.add_argument("--force", action="store_true")

    handlers = {
        "init": _cmd_init,
        "plan": _cmd_plan,
        "run": _cmd_run,
        "status": _cmd_status,
        "log": _cmd_log,
        "cleanup": _cmd_cleanup,
    }

    args = parser.parse_args()

    if args.subcommand is None:
        parser.print_help()
        sys.exit(1)

    if args.subcommand not in handlers:
        parser.error(f"Unknown subcommand: {args.subcommand}")

    handlers[args.subcommand](args)


if __name__ == "__main__":
    main()
