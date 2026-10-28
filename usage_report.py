"""
Measure how much the current Claude session has used, from its own transcript.

Scheduled tasks run this as their last step and log the result to the dashboard's
usage log. Prints one JSON object: token counts plus an "effective" number that
weights them the way they count against usage (cache reads ~0.1x, cache writes ~2x,
output ~5x a regular input token). The transcript is data only; nothing in it is run.

Usage: python3 -I usage_report.py <task name>
"""

import glob
import json
import os
import sys
from datetime import datetime, timezone


def main() -> int:
    task = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    files = glob.glob(os.path.join(base, "projects", "*", "*.jsonl"))
    if not files:
        print(json.dumps({"task": task, "error": "no transcript found"}))
        return 0
    main_file = max(files, key=os.path.getmtime)
    sub = glob.glob(os.path.join(main_file[:-6], "**", "*.jsonl"), recursive=True)
    seen = {}
    models = set()
    for path in [main_file] + sub:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                m = o.get("message") if isinstance(o, dict) else None
                if not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
                    continue
                key = m.get("id") or o.get("uuid") or str(len(seen))
                seen[key] = m["usage"]  # streamed copies of one message share an id; keep the last
                if m.get("model"):
                    models.add(m["model"])
    tot = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    for u in seen.values():
        tot["input"] += int(u.get("input_tokens") or 0)
        tot["cache_write"] += int(u.get("cache_creation_input_tokens") or 0)
        tot["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
        tot["output"] += int(u.get("output_tokens") or 0)
    effective = tot["input"] + 2 * tot["cache_write"] + 0.1 * tot["cache_read"] + 5 * tot["output"]
    print(json.dumps({
        "task": task,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": ", ".join(sorted(models)) or "unknown",
        "calls": len(seen),
        **{k + "_tokens": v for k, v in tot.items()},
        "effective_tokens": round(effective),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
