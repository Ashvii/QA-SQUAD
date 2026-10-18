#!/usr/bin/env python3
"""Dry run of the whole qa-squad pipeline against tests/fixtures/shop, using stand-in agents.

It drives qa.py exactly the way the orchestrator does: call `next`, then do what it says (run a
qa.py command, or "spawn" a wave of agents, played here by tests/standin.py). This repeats until
`next` says done.

    python tests/dry_run.py            # copies the fixture to a temp dir, runs, prints the summary
    python tests/dry_run.py --keep     # also keeps the temp dir and prints the REPORT.md path
"""

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "skills", "qa-squad"))
sys.path.insert(0, HERE)

import qa  # noqa: E402
import standin  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "shop")
NINE_LANES = "functional,race-conditions,data-integrity,authz-security,database,queue-cache-external," \
             "performance,chaos-ops,adversary"


def qa_call(argv):
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        code = qa.main(argv)
    if code:
        raise RuntimeError("qa.py %s failed (%s): %s" % (" ".join(argv), code, err.getvalue()))
    return buf.getvalue()


def copy_fixture(dest_parent=None):
    dest = os.path.join(tempfile.mkdtemp(prefix="qa-squad-dry-", dir=dest_parent), "shop")
    shutil.copytree(FIXTURE, dest)
    return dest


def init_run(project, lanes=NINE_LANES, depth=2, extra=()):
    argv = ["init", "--project", project, "--task-file", os.path.join(project, "scope.md"),
            "--target", "Shop checkout, order view and payment webhook", "--slug", "checkout"]
    if lanes:
        argv += ["--lanes", lanes]
    if depth:
        argv += ["--depth", str(depth)]
    return qa_call(argv + list(extra))


def drive(project, hunters=None, decider=None, max_steps=500, log=None):
    """Follow `qa.py next` until done. Returns (transcript lines, last command output)."""
    transcript = []
    last = ""
    for _ in range(max_steps):
        action = json.loads(qa_call(["next", "--json", "--project", project]))
        if action["action"] == "done":
            return transcript, last
        if action["action"] == "run":
            last = qa_call(action["argv"] + ["--project", project])
            transcript.append("$ qa.py " + " ".join(action["argv"]))
        else:
            transcript.append("~ spawn %d %s agent(s)" % (len(action["calls"]), action["kind"]))
            for call in action["calls"]:
                transcript.append("    " + standin.dispatch(project, action["kind"], call["brief"], hunters, decider))
        if log:
            log(transcript[-1])
    raise RuntimeError("pipeline did not finish in %d steps" % max_steps)


def current_state(project):
    with open(os.path.join(project, ".qa-squad", "CURRENT"), encoding="utf-8") as fh:
        run_id = fh.read().strip()
    run_dir = os.path.join(project, ".qa-squad", run_id)
    with open(os.path.join(run_dir, "state.json"), encoding="utf-8") as fh:
        return run_dir, json.load(fh)


def planted_bugs_flagged(state):
    """The three planted bugs, each as a live (non-disputed) final finding."""
    live = [f for f in state["findings"].values()
            if f["status"] == "final" and f.get("verdict") in ("CONFIRMED", "LIKELY")]
    checks = {
        "non-atomic stock decrement": lambda f: f["path"].endswith("CheckoutController.php") and "stock" in (
            f["title"] + f["root_cause"]).lower(),
        "IDOR on order show": lambda f: f["path"].endswith("OrderController.php") and f["category"] == "authz",
        "non-idempotent webhook": lambda f: f["path"].endswith("PaymentWebhookController.php") and "idempot" in (
            f["root_cause"]).lower(),
    }
    return {name: [f["id"] for f in live if test(f)] for name, test in checks.items()}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--keep", action="store_true", help="keep the temp project and print its path")
    args = ap.parse_args()

    project = copy_fixture()
    print("fixture copied to %s" % project)
    print(qa_call(["plan", "--lanes", NINE_LANES, "--depth", "2"]))
    print(init_run(project))
    transcript, report_out = drive(project)
    print("\n".join(transcript))
    print()
    print(report_out)
    run_dir, state = current_state(project)
    flagged = planted_bugs_flagged(state)
    ok = all(flagged.values()) and state["gate"]["result"] == "BLOCK"
    print("planted bugs flagged:")
    for name, ids in flagged.items():
        print("  %-28s %s" % (name, ", ".join(ids) or "MISSING"))
    print("gate: %s -> %s" % (state["gate"]["result"], "OK" if ok else "UNEXPECTED"))
    print("report: %s" % os.path.join(run_dir, "REPORT.md"))
    if not args.keep:
        shutil.rmtree(os.path.dirname(project), ignore_errors=True)
        print("(temp project removed; pass --keep to inspect it)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
