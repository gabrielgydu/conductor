"""Speccer CLI entry point."""
import argparse
import sys


def _cmd_init(args):
    print("Not implemented yet")


def _cmd_run(args):
    print("Not implemented yet")


def _cmd_generate(args):
    print("Not implemented yet")


def _cmd_status(args):
    print("Not implemented yet")


def _cmd_tree(args):
    print("Not implemented yet")


def main():
    parser = argparse.ArgumentParser(prog="speccer", description="Speccer specification tool")
    subparsers = parser.add_subparsers(dest="subcommand", metavar="subcommand")

    p_init = subparsers.add_parser("init", help="Initialize a new spec")
    p_init.add_argument("--feature", required=True)
    p_init.add_argument("--mode", default="backend")
    p_init.add_argument("--preset", default="base")

    p_run = subparsers.add_parser("run", help="Run speccer")
    p_run.add_argument("--continue", dest="cont", action="store_true")

    subparsers.add_parser("generate", help="Generate spec artifacts")
    subparsers.add_parser("status", help="Show spec status")
    subparsers.add_parser("tree", help="Show spec tree")

    handlers = {
        "init": _cmd_init,
        "run": _cmd_run,
        "generate": _cmd_generate,
        "status": _cmd_status,
        "tree": _cmd_tree,
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
