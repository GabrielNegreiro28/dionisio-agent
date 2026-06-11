"""
probe.py — dump raw Dionísio API data for ground-truth inspection.

This calls the API through the SAME path the agent uses (tool registry +
DionisioClient), so what you see here is exactly what the agent sees. Use it to
check whether an agent answer matches the underlying data — especially empty /
zero results, which are the easiest to get wrong silently.

Usage:
    python probe.py tools                       # list every tool + its params
    python probe.py <tool_name> key=value ...   # call one tool, print JSON
    python probe.py dump [--out DIR]            # dump the main collections to files

Examples:
    python probe.py clients_top_spenders period=month minSpent=500 limit=100
    python probe.py orders_list date=2026-06-09      # inspect order ITEM fields (Risoto check)
    python probe.py reservations_availability date=2026-06-10   # slots vs seats (Q1 check)
    python probe.py clients_inactive days=60
    python probe.py dump --out ./_dump
"""

import json
import os
import sys

from clients.dionisio import DionisioClient, DionisioAPIError
from tools.registry import get_tool
from tools.definitions import ALL_TOOLS

# Collections worth dumping wholesale for manual cross-checking.
_DUMP_PRESETS = {
    "clients_list":         {"limit": 500},
    "coupons_list":         {},
    "reservations_list":    {},
    "orders_list":          {"limit": 500},
    "clients_top_spenders": {"period": "month", "minSpent": 0, "limit": 500},
    "clients_inactive":     {"days": 60},
    "promotions_list":      {},
    "delivery_get_config":  {},
}


def _coerce(raw: str):
    """Turn a CLI string into the most likely JSON type."""
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _parse_kv(args: list[str]) -> dict:
    params = {}
    for a in args:
        if "=" not in a:
            sys.exit(f"Bad argument '{a}' — expected key=value")
        k, v = a.split("=", 1)
        params[k] = _coerce(v)
    return params


def _pretty(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _summary(data) -> str:
    """One-line shape hint so you can spot empty results fast."""
    if isinstance(data, list):
        return f"list[{len(data)}]"
    if isinstance(data, dict):
        for key in ("items", "results", "data"):
            if isinstance(data.get(key), list):
                return f"{{{key}: list[{len(data[key])}]}} keys={list(data.keys())}"
        return f"dict keys={list(data.keys())}"
    return type(data).__name__


def cmd_tools() -> None:
    for t in sorted(ALL_TOOLS, key=lambda x: x.name):
        props = t.parameters_schema.get("properties", {})
        required = set(t.parameters_schema.get("required", []))
        params = ", ".join(
            (f"{p}*" if p in required else p) for p in props
        ) or "(none)"
        flag = " [DESTRUCTIVE]" if t.destructive else ""
        print(f"{t.name:28} {t.method:5} {params}{flag}")


def cmd_call(tool_name: str, args: list[str]) -> None:
    try:
        tool_def = get_tool(tool_name)
    except KeyError:
        sys.exit(f"Unknown tool '{tool_name}'. Run: python probe.py tools")
    params = _parse_kv(args)
    client = DionisioClient()
    try:
        result = client.execute_tool(tool_def, params)
    except DionisioAPIError as e:
        sys.exit(f"API error [{e.status_code}]: {e.message}")
    print(f"# {tool_name} {params}  ->  {_summary(result)}\n")
    print(_pretty(result))


def cmd_dump(args: list[str]) -> None:
    out = "./_dump"
    if "--out" in args:
        i = args.index("--out")
        out = args[i + 1] if i + 1 < len(args) else out
    os.makedirs(out, exist_ok=True)
    client = DionisioClient()
    print(f"Dumping {len(_DUMP_PRESETS)} collections to {out}/\n")
    for name, params in _DUMP_PRESETS.items():
        try:
            result = client.execute_tool(get_tool(name), params)
        except DionisioAPIError as e:
            print(f"  {name:24} ERROR [{e.status_code}] {e.message}")
            continue
        path = os.path.join(out, f"{name}.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(_pretty(result))
        print(f"  {name:24} {_summary(result)}  ->  {path}")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "tools":
        cmd_tools()
    elif cmd == "dump":
        cmd_dump(sys.argv[2:])
    else:
        cmd_call(cmd, sys.argv[2:])


if __name__ == "__main__":
    main()
