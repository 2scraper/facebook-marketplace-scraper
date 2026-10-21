#!/usr/bin/env python3
"""Canary step: one line about a run's proxy traffic for the job summary.

    python3 .github/proxy_traffic_summary.py <out> <label> [budget_mb]

Reads `<out>.meta.json` and `<out>.latest_attempt.meta.json` (the newer of
the two when both exist), appends `**<label>**: proxy traffic X.X MB of
<budget> MB` to $GITHUB_STEP_SUMMARY, and exits 1 with an `::error::` line
when the run stopped at its proxy budget. A missing or unreadable sidecar,
or a run that used no local proxy, is reported and is not a failure here:
the canary's own check step judges the run itself.
"""
import json
import os
import sys


def _load(path):
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        print("::warning::%s is unreadable: %s" % (path, exc))
        return None


def summarise(out, label, budget_mb=10.0):
    """Return (line, whether the run stopped at its proxy budget)."""
    metas = [m for m in (_load(out + ".meta.json"), _load(out + ".latest_attempt.meta.json")) if m]
    meta = max(metas, key=lambda m: m.get("finished_at") or 0) if metas else None
    budget = "%g MB" % budget_mb if budget_mb else "no"
    if meta is None:
        line = "**%s**: proxy traffic not recorded (no sidecar for %s)" % (label, out)
    elif "proxy_bytes" not in meta:
        line = "**%s**: proxy traffic not recorded (the sidecar has no proxy_bytes)" % label
    elif meta.get("proxy_bytes") is None:
        line = "**%s**: proxy traffic: none (no local proxy on this run)" % label
    else:
        line = "**%s**: proxy traffic %.1f MB of %s" % (label, (meta.get("proxy_bytes") or 0) / 1e6, budget)
    exceeded = bool(meta and meta.get("proxy_budget_exceeded"))
    return line, exceeded


def main(argv):
    if len(argv) < 3:
        print("usage: proxy_traffic_summary.py <out> <label> [budget_mb]", file=sys.stderr)
        return 2
    try:
        budget_mb = float(argv[3]) if len(argv) > 3 else 10.0
    except ValueError:
        budget_mb = 10.0
    line, exceeded = summarise(argv[1], argv[2], budget_mb)
    print(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        except OSError as exc:
            print("::warning::could not write the job summary: %s" % exc)
    if exceeded:
        print("::error::stopped: proxy budget %g MB exceeded" % budget_mb)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
