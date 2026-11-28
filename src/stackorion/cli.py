"""Command-line access to the local catalog and dashboard."""

from __future__ import annotations

import argparse
import json
import os

from .catalog import Catalog, CatalogError, configured_path, set_enabled
from .dashboard_bridge import get_inspection, get_work_item, list_work_items, run_inspection
from .doctor import diagnose
from .queries import QueryRegistry


def main() -> None:
    parser = argparse.ArgumentParser(prog="stackorion")
    parser.add_argument("command", choices=["doctor", "inspection", "work-items", "work-item", "catalog", "accounts", "resources",
                                            "relations", "targets", "inspect", "discover", "metrics", "enable", "disable"])
    parser.add_argument("service_id", nargs="?")
    parser.add_argument("target_alias", nargs="?")
    parser.add_argument("--include-demo", action="store_true", help="include synthetic data in inspection and work item output")
    args = parser.parse_args()
    dashboard_commands = {"inspection", "work-items", "work-item"}
    if args.include_demo and args.command not in dashboard_commands:
        parser.error("--include-demo applies to inspection, work-items and work-item only")
    if args.command == "doctor":
        if args.service_id is not None:
            parser.error("doctor accepts no arguments; use STACKORION_CONFIG, STACKORION_STATE_DIR or STACKORION_DASHBOARD_URL")
        result = diagnose()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["status"] == "error":
            raise SystemExit(2)
        return
    try:
        if args.command in dashboard_commands:
            address = os.environ.get("STACKORION_DASHBOARD_URL", "http://127.0.0.1:8765")
            if args.command == "work-item":
                if args.service_id is None or args.target_alias is not None:
                    parser.error("work-item requires a work item ID")
                result = get_work_item(args.service_id, include_demo=args.include_demo, address=address)
            elif args.command == "work-items":
                if args.service_id is not None:
                    parser.error("work-items accepts no positional arguments")
                result = list_work_items(args.include_demo, address=address)
            else:
                if args.service_id not in (None, "latest", "run") or args.target_alias is not None:
                    parser.error("inspection accepts latest or run")
                query = run_inspection if args.service_id == "run" else get_inspection
                result = query(args.include_demo, address=address)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        catalog = Catalog.load(configured_path())
        if args.command == "catalog":
            result = catalog.list()
        elif args.command == "accounts":
            result = catalog.accounts()
        elif args.command in {"enable", "disable"}:
            if not args.service_id:
                parser.error("enable/disable requires service_id")
            set_enabled(configured_path(), args.service_id, args.command == "enable")
            result = {"service": args.service_id, "enabled": args.command == "enable"}
        elif args.command == "resources":
            result = catalog.resources(args.service_id)
        elif args.command == "relations":
            if not args.service_id:
                parser.error("relations requires resource_id")
            result = catalog.relations(args.service_id)
        elif args.command == "discover":
            if not args.service_id:
                parser.error("discover requires service_id")
            adapter = catalog.adapter(catalog.get(args.service_id))
            if not hasattr(adapter, "discover"):
                raise CatalogError("service does not support discovery")
            result = adapter.discover()
        elif args.command == "targets":
            if not args.service_id:
                parser.error("targets requires service_id")
            result = catalog.adapter(catalog.get(args.service_id)).list_targets()
        elif args.command == "metrics":
            if not args.service_id or not args.target_alias:
                parser.error("metrics requires service_id and target_alias")
            result = QueryRegistry(catalog).query(args.service_id, "metrics", {"alias": args.target_alias})
        else:
            if not args.service_id or not args.target_alias:
                parser.error("inspect requires service_id and target_alias")
            result = catalog.adapter(catalog.get(args.service_id)).inspect(args.target_alias)
    except CatalogError as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
