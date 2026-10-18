"""Tests for skills/qa-squad/qa.py. Standard library only.

    python3 -m unittest discover -s tests -v

Sub-agents are played by tests/standin.py, which reads the briefs qa.py writes and produces canned
finding/verdict files for the fixture project in tests/fixtures/shop.
"""

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SKILL = os.path.join(ROOT, "skills", "qa-squad")
sys.path.insert(0, SKILL)
sys.path.insert(0, HERE)

import qa  # noqa: E402
import bench  # noqa: E402
import http.server  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import standin  # noqa: E402
import dry_run  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "shop")


def call(argv):
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        code = qa.main(argv)
    return code, buf.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qa-squad-test-")
        self.project = os.path.join(self.tmp, "shop")
        shutil.copytree(FIXTURE, self.project)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def qa(self, *argv):
        argv = list(argv) + ["--project", self.project]
        code, out, err = call(argv)
        self.assertEqual(code, 0, "qa.py %s failed: %s" % (" ".join(argv), err))
        return out

    def qa_fail(self, *argv):
        code, out, err = call(list(argv) + ["--project", self.project])
        self.assertEqual(code, 2, "expected failure, got output: %s" % out)
        return err

    def init(self, *extra, **kw):
        scope = kw.get("scope", os.path.join(self.project, "scope.md"))
        return self.qa("init", "--task-file", scope, "--slug", kw.get("slug", "t"), *extra)

    def ctx(self):
        return qa.Ctx(self.project, self.run_id())

    def run_id(self):
        with open(os.path.join(self.project, ".qa-squad", "CURRENT"), encoding="utf-8") as fh:
            return fh.read().strip()

    def run_path(self, *parts):
        return os.path.join(self.project, ".qa-squad", self.run_id(), *parts)

    def state(self):
        with open(self.run_path("state.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def next(self):
        return json.loads(self.qa("next", "--json"))

    def write(self, rel, text):
        path = self.run_path(*rel.split("/"))
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        return path

    def hunt_all(self, hunters=None):
        """Play every pending hunter until validate is next."""
        while True:
            action = self.next()
            if action["action"] == "spawn" and action["kind"] == "hunt":
                for c in action["calls"]:
                    standin.hunt(self.project, c["brief"], hunters)
            elif action["action"] == "run" and action["argv"][0] == "prompts":
                self.qa(*action["argv"])
            else:
                return action


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

class TestPlan(Base):
    def plan(self, *argv):
        return json.loads(self.qa("plan", "--json", *argv))

    def test_quick(self):
        p = self.plan("--quick")
        self.assertEqual((p["lanes"], p["hunters"], p["skeptics"], p["total"]), (5, 5, 25, 30))
        self.assertEqual((p["hunt_waves"], p["verify_waves"], p["waves"]), (1, 3, 4))
        self.assertFalse(p["needs_consent"])

    def test_default_all_lanes(self):
        p = self.plan()
        self.assertEqual((p["lanes"], p["hunters"], p["skeptics"], p["total"]), (11, 22, 110, 132))
        self.assertEqual(p["waves"], 3 + 11)
        self.assertTrue(p["needs_consent"])

    def test_aliases_and_depth(self):
        p = self.plan("--lanes", "race,authz,queue,db,perf", "--depth", "3")
        self.assertEqual([r["lane"] for r in p["rows"]],
                         ["race-conditions", "authz-security", "database", "queue-cache-external", "performance"])
        self.assertEqual((p["hunters"], p["skeptics"], p["total"]), (15, 75, 90))
        self.assertTrue(p["needs_consent"])
        self.assertEqual((p["hunt_waves"], p["verify_waves"]), (2, 8))
        p = self.plan("--lanes", "race,authz,queue", "--depth", "2", "--est-findings", "4")
        self.assertEqual(p["total"], 6 + 24)
        p = self.plan("--lanes", "race,authz,queue,db,perf", "--depth", "2", "--est-findings", "5")
        self.assertEqual(p["total"], 60)
        self.assertFalse(p["needs_consent"], "exactly 60 calls does not need consent")

    def test_no_verify_and_skip_and_estimate(self):
        p = self.plan("--skip", "ui-ux", "--depth", "1", "--no-verify")
        self.assertEqual((p["lanes"], p["hunters"], p["skeptics"], p["total"], p["waves"]), (10, 10, 0, 10, 1))
        p = self.plan("--skip", "ui,perf", "--est-findings", "2")
        self.assertEqual((p["lanes"], p["hunters"], p["skeptics"], p["total"]), (9, 18, 36, 54))

    def test_wave_size(self):
        p = self.plan("--quick", "--wave", "2")
        self.assertEqual((p["hunt_waves"], p["verify_waves"]), (3, 13))

    def test_rejects_bad_input(self):
        self.assertIn("--depth", self.qa_fail("plan", "--depth", "6"))
        self.assertIn("unknown lane", self.qa_fail("plan", "--lanes", "nonsense"))

    def test_plan_writes_nothing_and_matches_init(self):
        p = self.plan("--lanes", "race,authz,func", "--depth", "2")
        self.assertFalse(os.path.exists(os.path.join(self.project, ".qa-squad")))
        self.init("--lanes", "race,authz,func", "--depth", "2")
        self.assertEqual(len(self.state()["jobs"]["hunt"]), p["hunters"])

    def test_text_table(self):
        out = self.qa("plan", "--quick")
        self.assertIn("sub-agent calls (expected): 30 = 5 hunters + 25 skeptics", out)
        self.assertIn("waves: 4 (1 hunt + 3 verify", out)


# ---------------------------------------------------------------------------
# scope hashing and briefs
# ---------------------------------------------------------------------------

def extract_scope(brief_bytes):
    text = brief_bytes.decode("utf-8", errors="surrogateescape")
    start = text.index("-----BEGIN SCOPE.MD")
    start = text.index("\n", start) + 1
    end = text.rindex(qa.SCOPE_END)
    return text[start:end].encode("utf-8", errors="surrogateescape")


class TestScopeAndBriefs(Base):
    def test_sha_recorded_and_scope_byte_identical_in_every_brief(self):
        scope = os.path.join(self.tmp, "scope-crlf.md")
        raw = "# Checkout é 🚀\r\n\r\nLine with trailing spaces   \r\n{lane} {run} literal braces\r\nno final newline".encode()
        with open(scope, "wb") as fh:
            fh.write(raw)
        self.init("--lanes", "race,queue,func", "--depth", "2", scope=scope)
        st = self.state()
        self.assertEqual(st["scope_sha256"], hashlib.sha256(raw).hexdigest())
        with open(self.run_path("scope.md"), "rb") as fh:
            self.assertEqual(fh.read(), raw)

        self.hunt_all()
        self.qa("validate")
        self.qa("prompts", "verify")
        st = self.state()
        briefs = [j["brief_path"] for k in ("hunt", "verify") for j in st["jobs"][k]]
        self.assertEqual(len(st["jobs"]["hunt"]), 6)
        self.assertGreaterEqual(len(st["jobs"]["verify"]), 3)
        for rel in briefs:
            with open(os.path.join(self.project, *rel.split("/")), "rb") as fh:
                self.assertEqual(extract_scope(fh.read()), raw, rel)

    def test_skeptic_brief_hides_hunter_reasoning(self):
        self.init("--lanes", "queue", "--depth", "1")
        self.hunt_all()
        self.qa("validate")
        self.qa("prompts", "verify")
        job = self.state()["jobs"]["verify"][0]
        with open(os.path.join(self.project, *job["brief_path"].split("/")), encoding="utf-8") as fh:
            brief = fh.read()
        self.assertIn("Duplicate payment webhook delivery double-applies a payment", brief)   # title
        self.assertIn("A provider retry creates a second payment row", brief)                 # what breaks
        self.assertIn('{"payment_id":"pay_123"', brief)                                       # trigger
        self.assertIn("app/Http/Controllers/PaymentWebhookController.php:22", brief)          # location
        self.assertNotIn("inserts unconditionally", brief)                                    # Why section
        self.assertNotIn("firstOrCreate inside a transaction", brief)                         # Suggested fix
        self.assertNotIn("findings/queue-cache-external-1-1.md", brief)

    def test_hunter_brief_content(self):
        self.init("--lanes", "race", "--depth", "1")
        self.qa("prompts", "hunt")
        with open(self.run_path("briefs", "hunt", "race-conditions-1.md"), encoding="utf-8") as fh:
            brief = fh.read()
        self.assertIn('You are a QA hunter in lane "race-conditions" (hunter 1 of 1). Angle: code-reading', brief)
        self.assertIn("  4. Concurrent stock or balance deductions", brief)
        self.assertIn("HARD RULE: a single-threaded test", brief)
        self.assertIn('Reply with ONE line: "race-conditions-1: wrote K findings"', brief)
        self.assertIn("findings/race-conditions-1.done", brief)
        self.assertIn("SAFETY RULES", brief)
        self.assertIn("## Suggested test", brief)

    def test_tampered_scope_is_refused(self):
        self.init("--lanes", "func", "--depth", "1")
        with open(self.run_path("scope.md"), "ab") as fh:
            fh.write(b"\nsneaky edit\n")
        self.assertIn("scope.md changed", self.qa_fail("prompts", "hunt"))

    def test_skill_md_quotes_briefs_verbatim(self):
        with open(os.path.join(SKILL, "SKILL.md"), encoding="utf-8") as fh:
            skill = fh.read()
        self.assertIn(qa.HUNTER_BRIEF, skill)
        self.assertIn(qa.HUNTER_BRIEF_EXTRA, skill)
        self.assertIn(qa.SKEPTIC_BRIEF, skill)
        self.assertTrue(skill.startswith("---\nname: qa-squad\n"))
        self.assertIn("disable-model-invocation: false", skill)

    def test_angle_dealing(self):
        self.assertEqual(qa.deal_angles("functional", 2), ["code-reading", "test-first"])
        self.assertEqual(sorted(qa.deal_angles("functional", 5)), sorted(a for a, _ in qa.ANGLES))
        self.assertEqual(qa.deal_angles("adversary", 3, seed="42"), qa.deal_angles("adversary", 3, seed="42"))
        seeded = {lane: qa.deal_angles(lane, 1, seed="7")[0] for lane in ("functional", "race-conditions",
                                                                           "data-integrity", "authz-security",
                                                                           "database", "adversary")}
        self.assertGreater(len(set(seeded.values())), 1, "a seed should rotate starting angles across lanes")

    def test_run_id_format(self):
        self.init("--lanes", "func", "--depth", "1", slug="Checkout Flow!")
        self.assertRegex(self.run_id(), r"^\d{8}-\d{6}-checkout-flow$")


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

CHECKOUT = "app/Http/Controllers/CheckoutController.php"


def good(**kw):
    values = dict(
        lane="functional", title="Checkout returns 500 for a deleted product", severity="HIGH", category="logic",
        location=CHECKOUT + ":17", root_cause="findOrFail on soft-deleted product",
        what="A deleted product id gives a 500 instead of a 422.",
        why="- %s:17 calls findOrFail with the raw id" % CHECKOUT,
        trigger='POST /api/checkout {"product_id": 999, "quantity": 1} after product 999 was deleted.',
        expected="Expected: 422\nActual: 500",
        test="```php\n$this->postJson('/api/checkout', ['product_id' => 999])->assertUnprocessable();\n```",
        fix="Validate exists:products,id,deleted_at,NULL.")
    values.update(kw)
    return standin.finding_md(**values)


class TestValidate(Base):
    def setUp(self):
        super(TestValidate, self).setUp()
        self.init("--lanes", "func,race", "--depth", "2")
        self.qa("prompts", "hunt")

    def rejected(self):
        return {f["file"].rsplit("/", 1)[1]: f["reject_reason"] for f in self.state()["findings"].values()
                if f["status"] == "rejected"}

    def test_rules(self):
        cases = {
            "functional-1-1.md": good(),
            "functional-1-2.md": good().replace("root_cause: findOrFail on soft-deleted product\n", ""),
            "functional-1-3.md": good(severity="URGENT"),
            "functional-1-4.md": good(location="app/Services/Nope.php:3"),
            "functional-1-5.md": good(title="Checkout might be unsafe",
                                      trigger="A user could potentially break the checkout somehow in some cases."),
            "functional-1-6.md": good(test="<!-- write a test here -->"),
            "functional-1-7.md": good(severity="CRITICAL", why="The controller trusts the id."),
            "functional-1-8.md": good().split("## Suggested fix")[0],
            "functional-1-9.md": good(trigger="POST /api/checkout twice"),
            "functional-2-1.md": good(location="no line number here"),
            "functional-2-2.md": good(lane="nonsense-lane"),
            "functional-2-3.md": good(trigger="Somebody does something unusual with this checkout endpoint form"),
        }
        for name, text in cases.items():
            self.write("findings/" + name, text)
        self.qa("validate", "--force")
        rej = self.rejected()
        expect = {
            "functional-1-2.md": "missing frontmatter field(s): root_cause",
            "functional-1-3.md": "bad severity 'URGENT'",
            "functional-1-4.md": "location file does not exist",
            "functional-1-5.md": "hedging-only trigger",
            "functional-1-6.md": "empty suggested test",
            "functional-1-7.md": "CRITICAL finding without file:line",
            "functional-1-8.md": "missing section: ## Suggested fix",
            "functional-1-9.md": "exact trigger too short",
            "functional-2-1.md": "has no file:line",
            "functional-2-2.md": "unknown lane",
            "functional-2-3.md": "vague trigger",
        }
        for name, reason in expect.items():
            self.assertIn(name, rej, name)
            self.assertIn(reason, rej[name], name)
            self.assertTrue(os.path.isfile(self.run_path("findings", "_rejected", name)))
            self.assertTrue(os.path.isfile(self.run_path("findings", "_rejected", name + ".reason.txt")))
        self.assertNotIn("functional-1-1.md", rej)
        st = self.state()
        self.assertEqual(st["findings"]["FN-001"]["file"].rsplit("/", 1)[1], "functional-1-1.md")
        with open(self.run_path("findings", "functional-1-1.md"), encoding="utf-8") as fh:
            self.assertIn("\nid: FN-001\n", fh.read())

    def test_over_cap_rejects_lowest_severity(self):
        sevs = ["LOW", "HIGH", "MEDIUM", "LOW", "MEDIUM", "HIGH", "MEDIUM", "MEDIUM", "MEDIUM", "HIGH"]
        for k, sev in enumerate(sevs, 1):
            self.write("findings/race-conditions-1-%d.md" % k,
                       good(lane="race-conditions", severity=sev, title="Finding number %d" % k))
        self.qa("validate", "--force")
        rej = self.rejected()
        self.assertEqual(sorted(rej), ["race-conditions-1-1.md", "race-conditions-1-4.md"])
        self.assertTrue(all(r.startswith("over-cap") for r in rej.values()))
        accepted = [f for f in self.state()["findings"].values() if f["status"] == "new"]
        self.assertEqual(len(accepted), 8)

    def test_fix_pass_spawned_once_when_lane_mostly_rejected(self):
        self.write("findings/functional-1-1.md", good())
        self.write("findings/functional-1-2.md", good(severity="URGENT"))
        self.write("findings/functional-2-1.md", good(test=""))
        self.write("findings/race-conditions-1-1.md", good(lane="race-conditions", severity="BAD"))
        self.write("findings/race-conditions-1-2.md", good(lane="race-conditions", title="A real one"))
        for job in ("functional-1", "functional-2", "race-conditions-1", "race-conditions-2"):
            self.write("findings/%s.done" % job, "findings: 1\ncovered: 1, 2\n")
        self.qa("validate")
        st = self.state()
        self.assertEqual(st["retried_lanes"], ["functional"], "2/3 rejected > 50%; race is exactly 50%")
        self.assertEqual(st["phase"], "hunting")
        action = self.next()
        self.assertEqual([c["id"] for c in action["calls"]], ["functional-fix"])
        with open(self.run_path("briefs", "hunt", "functional-fix.md"), encoding="utf-8") as fh:
            brief = fh.read()
        self.assertIn("FIX PASS", brief)
        self.assertIn("bad severity 'URGENT'", brief)
        self.assertIn("empty suggested test", brief)
        # The fix hunter writes junk again: no second fix pass.
        self.write("findings/functional-fix-1.md", good(severity="NOPE"))
        self.write("findings/functional-fix.done", "findings: 1\ncovered: 1\n")
        self.qa("validate", "--force")
        st = self.state()
        self.assertEqual(st["retried_lanes"], ["functional"])
        self.assertEqual(len([j for j in st["jobs"]["hunt"] if j["fix"]]), 1)
        self.assertEqual(st["phase"], "verifying")

    def test_refuses_while_hunters_pending(self):
        self.assertIn("still pending", self.qa_fail("validate"))


# ---------------------------------------------------------------------------
# dedupe
# ---------------------------------------------------------------------------

def mk(fid, lane, path, line, category, title, root, verdict, severity):
    return {"id": fid, "lane": lane, "hunter": lane + "-1", "path": path, "line": line,
            "location": "%s:%d" % (path, line), "category": category, "title": title, "root_cause": root,
            "verdict": verdict, "severity": severity, "orig_severity": severity, "status": "new",
            "merged_from": [], "merged_into": None, "found_by_lanes": [lane], "multi_lane": False,
            "file": "x", "test": ""}


CHECKOUT_SRC = """<?php
class C
{
    public function store($request)
    {
        // line 6
        $product = Product::find(1);
        //
        //
        //
        //
        //
        //
        //
        //
        //
        //
        //
        //
        //
        //
        //
        if ($product->stock < 1) {}          // line 24
        //
        //
        //
        //
        //
        $product->save();                     // line 31
    }
}
"""

# Two different authz bugs 6 lines apart, in different methods of the same controller.
TWO_METHODS_SRC = """<?php
class TeamController
{
    public function show($id)
    {
        return Team::findOrFail($id);         // line 6: IDOR on show
    }

    public function update($request, $id)
    {
        $team = Team::findOrFail($id);
        $team->update($request->all());       // line 12: mass assignment of owner_id
    }
}
"""


class TestDedupe(Base):
    def source(self, rel, text):
        path = os.path.join(self.project, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def dedupe(self, findings):
        self.init("--lanes", "func", "--depth", "1")
        ctx = self.ctx()
        ctx.state["findings"] = {f["id"]: f for f in findings}
        ctx.state["phase"] = "deduping"
        ctx.save()
        self.qa("dedupe")
        return self.state()["findings"]

    def test_merge_rules(self):
        C, O = "app/C.php", "app/O.php"
        self.source(C, CHECKOUT_SRC)
        fs = self.dedupe([
            mk("RC-001", "race-conditions", C, 24, "race", "Overselling", "non-atomic decrement on stock",
               "LIKELY", "CRITICAL"),
            mk("DI-001", "data-integrity", "app/Other.php", 5, "integrity", "Negative stock",
               "Non-atomic  decrement on STOCK.", "CONFIRMED", "HIGH"),                       # rule 1
            mk("DB-001", "database", C, 31, "race", "No row lock", "decrement on stock without lockForUpdate",
               "LIKELY", "MEDIUM"),
            mk("AZ-001", "authz-security", O, 30, "authz", "Any user can read another user's order",
               "missing ownership check in show", "CONFIRMED", "CRITICAL"),
            mk("AD-001", "adversary", O, 90, "logic", "Any user can read another user's order by id",
               "no ownership check on the order show action", "LIKELY", "HIGH"),               # rule 3
            mk("FN-001", "functional", C, 200, "validation", "Quantity zero accepted", "no min rule",
               "LIKELY", "LOW"),                                                                # unrelated
            mk("FN-002", "functional", C, 26, "race", "Stock race again", "non-atomic decrement on stock",
               "DISPUTED", "CRITICAL"),                                                         # disputed
            mk("PF-001", "performance", "app/P.php", 1, "perf", "Slow list", "n+1", "NEEDS-ENV", "MEDIUM"),
        ])
        # rule 1 + rule 2 chain; keeper is the CONFIRMED one, severity raised to the group max
        self.assertEqual(fs["DI-001"]["status"], "final")
        self.assertEqual(fs["DI-001"]["severity"], "CRITICAL")
        self.assertEqual(fs["DI-001"]["merged_from"], ["DB-001", "RC-001"])
        self.assertEqual(fs["DI-001"]["found_by_lanes"], ["race-conditions", "data-integrity", "database"])
        self.assertTrue(fs["DI-001"]["multi_lane"])
        self.assertEqual(fs["RC-001"]["merged_into"], "DI-001")
        self.assertEqual(fs["DB-001"]["status"], "merged")
        # rule 3 (fuzzy title, same file, far apart, different category)
        self.assertEqual(fs["AZ-001"]["merged_from"], ["AD-001"])
        self.assertTrue(fs["AZ-001"]["multi_lane"])
        # unrelated stays separate
        for fid in ("FN-001", "PF-001"):
            self.assertEqual(fs[fid]["status"], "final")
            self.assertFalse(fs[fid]["multi_lane"])
        # DISPUTED never absorbs and is never absorbed
        self.assertEqual(fs["FN-002"]["status"], "final")
        self.assertEqual(fs["FN-002"]["merged_from"], [])

    def test_same_issue_rules_directly(self):
        a = mk("A", "x", "f.php", 10, "race", "Title one", "root one", "LIKELY", "LOW")
        same_fn = lambda f: "store"  # noqa: E731
        unknown_fn = lambda f: None  # noqa: E731
        self.assertEqual(qa.same_issue(a, mk("B", "y", "g.php", 99, "authz", "Other", "Root ONE!", "LIKELY", "LOW")),
                         "same-root-cause")
        a = mk("A", "x", "f.php", 10, "race", "Title one", "stock decrement is not atomic", "LIKELY", "LOW")
        # within 10 lines + same function needs a clearly shared root cause; category no longer matters
        b = mk("B", "y", "f.php", 18, "integrity", "Other", "atomic stock decrement missing", "LIKELY", "LOW")
        self.assertEqual(qa.same_issue(a, b, same_fn), "same-function")
        self.assertEqual(qa.same_issue(a, b, unknown_fn), "same-function", "unknown function falls back to wording")
        self.assertIsNone(qa.same_issue(a, b, lambda f: "store" if f["id"] == "A" else "update"),
                          "nearby but in different functions")
        unrelated = mk("C", "y", "f.php", 18, "race", "Other", "missing failed() handler", "LIKELY", "LOW")
        self.assertIsNone(qa.same_issue(a, unrelated, same_fn), "same function, unrelated root cause")
        # within 3 lines: any shared root-cause word is enough
        spot = mk("D", "y", "f.php", 12, "perf", "Other", "decrement uses stale read", "LIKELY", "LOW")
        self.assertEqual(qa.same_issue(a, spot, same_fn), "same-spot")
        far = mk("E", "y", "f.php", 21, "race", "Other", "stock decrement is atomic-ish", "LIKELY", "LOW")
        self.assertNotIn(qa.same_issue(a, far, same_fn), ("same-spot", "same-function"), "more than 10 lines apart")
        # different files never merge on wording alone (two IDORs in two controllers need two fixes)
        self.assertIsNone(qa.same_issue(
            mk("F", "x", "app/OrderController.php", 30, "authz", "IDOR", "show() loads any order by id without an "
               "ownership check", "LIKELY", "LOW"),
            mk("G", "x", "app/InvoiceController.php", 30, "authz", "IDOR", "show() loads any invoice by id without "
               "an ownership check", "LIKELY", "LOW")))

    def test_real_run_regression(self):
        """Root causes from a real 7-lane run on the download-centre fixture.

        handle() holds four different bugs within 13 lines; the old category/same-function rule chained them all
        into one finding. Same-line duplicates from different lanes used different categories and did not merge.
        """
        J = "app/Jobs/GenerateExportJob.php"
        P = "routes/console.php"
        self.source(J, "<?php\nclass GenerateExportJob\n{\n    public function handle(): void\n    {\n"
                    + "        //\n" * 40 + "    }\n}\n")
        self.source(P, "<?php\n" + "//\n" * 20)
        rows = [
            ("DI-001", "data-integrity", J, 33, "integrity", "BelongsToTenant global scope only applies when "
             "currentTenantId is bound, and nothing binds it inside the queue worker"),
            ("QC-001", "queue-cache-external", J, 33, "queue", "tenant global scope depends on currentTenantId, "
             "which only SetTenant middleware binds; the queue worker never binds it"),
            ("AZ-002", "authz-security", J, 33, "authz", "tenant scope depends on the request-only currentTenantId "
             "binding, which is never set in the Redis queue worker"),
            ("DI-002", "data-integrity", J, 42, "integrity", "CSV opened in append mode ('a') at a deterministic "
             "path, so each retry or overlapping attempt appends to the previous file"),
            ("QC-002", "queue-cache-external", J, 42, "queue", "CSV opened in append mode ('a') at a deterministic "
             "path, so every attempt appends to the previous attempt's partial file"),
            ("DI-003", "data-integrity", J, 29, "integrity", "job sets status=processing but has no failed() handler "
             "or catch block to set a terminal failed state"),
            ("FN-003", "functional", J, 29, "logic", "job sets status=processing first and has no failed() handler "
             "or try/catch to set a terminal failed status"),
            ("PF-001", "performance", J, 36, "perf", "unbounded Invoice::query()->get() materialises the full "
             "result set as Eloquent models before writing the CSV"),
            ("FN-007", "functional", J, 36, "perf", "->get() hydrates the entire result set as Eloquent models "
             "instead of streaming with cursor()/lazy()"),
            ("DI-004", "data-integrity", P, 8, "integrity", "Builder::each() paginates with offset (chunk) while the "
             "callback deletes rows from the same result set"),
            ("AD-006", "adversary", P, 8, "logic", "Builder::each() chunks with LIMIT/OFFSET while the callback "
             "deletes rows from the same result set"),
            ("PF-004", "performance", P, 8, "perf", "Builder::each() pages with LIMIT/OFFSET while the callback "
             "deletes rows from the same result set; also no index on created_at"),
        ]
        fs = self.dedupe([mk(i, lane, path, line, cat, "t " + i, root, "CONFIRMED", "HIGH")
                          for i, lane, path, line, cat, root in rows])
        groups = {}
        for f in fs.values():
            groups.setdefault(f.get("merged_into") or f["id"], set()).add(f["id"])
        expected = [{"AZ-002", "DI-001", "QC-001"}, {"DI-002", "QC-002"}, {"DI-003", "FN-003"},
                    {"FN-007", "PF-001"}, {"AD-006", "DI-004", "PF-004"}]
        self.assertEqual(sorted(map(sorted, groups.values())), sorted(map(sorted, expected)))

    def test_nearby_bugs_in_different_methods_stay_separate(self):
        T = "app/Http/Controllers/TeamController.php"
        self.source(T, TWO_METHODS_SRC)
        fs = self.dedupe([
            mk("AZ-001", "authz-security", T, 6, "authz", "Any member can read any team", "show() has no policy",
               "CONFIRMED", "CRITICAL"),
            mk("AZ-002", "authz-security", T, 12, "authz", "Member can set owner_id on update",
               "mass assignment via request->all()", "LIKELY", "HIGH"),
        ])
        self.assertEqual((fs["AZ-001"]["status"], fs["AZ-002"]["status"]), ("final", "final"))

    def test_nearby_bugs_in_same_method_merge(self):
        T = "app/Http/Controllers/TeamController.php"
        self.source(T, TWO_METHODS_SRC)
        fs = self.dedupe([
            mk("AZ-001", "authz-security", T, 11, "authz", "Update loads any team",
               "update() has no ownership check", "LIKELY", "HIGH"),
            mk("AD-001", "adversary", T, 12, "logic", "Editing someone else's team",
               "missing ownership check in update()", "LIKELY", "MEDIUM"),
        ])
        self.assertEqual(fs["AZ-001"]["merged_from"], ["AD-001"])
        self.assertEqual(self.state()["dedupe_rules"], ["AD-001 ~ AZ-001 (same-spot)"])

    def test_enclosing_function_detection(self):
        self.source("app/x.py", "class A:\n    def first(self):\n        pass\n\n    async def second(self):\n        x = 1\n")
        self.source("app/x.go", "package x\n\nfunc (s *Svc) Charge(id int) {\n\tdo()\n}\n")
        cache = {}
        self.assertEqual(qa.enclosing_function(self.project, "app/x.py", 3, cache), "first")
        self.assertEqual(qa.enclosing_function(self.project, "app/x.py", 6, cache), "second")
        self.assertEqual(qa.enclosing_function(self.project, "app/x.go", 4, cache), "Charge")
        self.assertIsNone(qa.enclosing_function(self.project, "app/missing.php", 4, cache))

    def test_disputed_does_not_absorb(self):
        fs = self.dedupe([
            mk("RC-001", "race-conditions", "a.php", 1, "race", "T", "same root", "DISPUTED", "CRITICAL"),
            mk("DI-001", "data-integrity", "a.php", 2, "race", "T", "same root", "UNVERIFIED", "LOW"),
            mk("DB-001", "database", "a.php", 3, "race", "T", "same root", "DISPUTED", "HIGH"),
        ])
        self.assertEqual(fs["DI-001"]["status"], "final")
        self.assertEqual(fs["DI-001"]["merged_from"], [])
        self.assertEqual(fs["DI-001"]["severity"], "LOW")
        self.assertEqual(fs["RC-001"]["merged_from"], ["DB-001"], "disputed findings may merge with each other")


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------

def verdict(fid, v, evidence, parallel="no", test="none"):
    return standin.verdict_md(fid, v, evidence, "Reasoning text.", test=test, parallel=parallel)


class TestCollect(Base):
    def setUp(self):
        super(TestCollect, self).setUp()
        self.init("--lanes", "race,queue", "--depth", "1")
        self.hunt_all()
        self.qa("validate")
        self.qa("prompts", "verify")
        st = self.state()
        self.ids = sorted(st["findings"])
        self.assertEqual(self.ids, ["QC-001", "RC-001"])

    def test_verdict_without_file_line_is_requeued_once_then_needs_env(self):
        self.write("verdicts/QC-001.md", verdict("QC-001", "CONFIRMED", "trust me"))
        self.write("verdicts/RC-001.md", verdict("RC-001", "DISPUTED", CHECKOUT + ":20"))
        self.qa("collect")
        st = self.state()
        job = [j for j in st["jobs"]["verify"] if j["id"] == "QC-001"][0]
        self.assertEqual((job["status"], job["requeues"]), ("pending", 1))
        self.assertIsNone(st["findings"]["QC-001"]["verdict"])
        self.assertEqual(st["findings"]["RC-001"]["verdict"], "DISPUTED")
        self.assertTrue(os.path.isfile(self.run_path("verdicts", "_rejected", "QC-001.attempt1.md")))
        self.assertEqual(st["phase"], "verifying")

        action = self.next()
        self.assertEqual([c["id"] for c in action["calls"]], ["QC-001"])
        with open(self.run_path("briefs", "verify", "QC-001.md"), encoding="utf-8") as fh:
            self.assertIn("YOUR PREVIOUS VERDICT FOR THIS FINDING WAS REJECTED: no file:line evidence", fh.read())

        self.write("verdicts/QC-001.md", verdict("QC-001", "LIKELY", "still no citation"))
        self.qa("collect")
        st = self.state()
        self.assertEqual(st["findings"]["QC-001"]["verdict"], "NEEDS-ENV")
        self.assertIn("rejected twice", st["findings"]["QC-001"]["verdict_note"])
        self.assertEqual(st["phase"], "deduping")

    def test_latent_verdict_is_recorded(self):
        self.write("verdicts/QC-001.md", verdict("QC-001", "LATENT", CHECKOUT + ":24"))
        self.write("verdicts/RC-001.md", verdict("RC-001", "LIKELY", CHECKOUT + ":24"))
        self.qa("collect")
        self.assertEqual(self.state()["findings"]["QC-001"]["verdict"], "LATENT")

    def test_invalid_verdict_value_is_rejected(self):
        self.write("verdicts/QC-001.md", verdict("QC-001", "PROBABLY", CHECKOUT + ":20"))
        self.write("verdicts/RC-001.md", verdict("RC-001", "LIKELY", CHECKOUT + ":20"))
        self.qa("collect")
        job = [j for j in self.state()["jobs"]["verify"] if j["id"] == "QC-001"][0]
        self.assertEqual(job["requeue_reason"], "invalid verdict value 'PROBABLY'")

    def test_race_confirmed_without_parallel_marker_is_downgraded(self):
        self.write("verdicts/RC-001.md", verdict("RC-001", "CONFIRMED", CHECKOUT + ":24", parallel="no"))
        self.write("verdicts/QC-001.md", verdict("QC-001", "CONFIRMED", CHECKOUT + ":24", parallel="no"))
        self.qa("collect")
        fs = self.state()["findings"]
        self.assertEqual(fs["RC-001"]["verdict"], "LIKELY")
        self.assertIn("downgraded", fs["RC-001"]["verdict_note"])
        self.assertEqual(fs["QC-001"]["verdict"], "CONFIRMED", "non-race findings need no parallel marker")

    PARALLEL_TEST = ("<?php\nit('never oversells', function () {\n"
                     "    $results = Process::pool(fn ($pool) => [$pool->command('php checkout.php'),\n"
                     "                                            $pool->command('php checkout.php')])->start()->wait();\n"
                     "    expect(Product::find(7)->stock)->toBe(0);\n});\n")
    SEQUENTIAL_TEST = ("<?php\nit('never oversells', function () {\n"
                       "    $this->postJson('/api/checkout', $body);\n    $this->postJson('/api/checkout', $body);\n"
                       "    expect(Product::find(7)->stock)->toBe(0);\n});\n")

    def race_verdict(self, test, parallel="yes"):
        self.write("verdicts/RC-001.md", verdict("RC-001", "CONFIRMED", CHECKOUT + ":24", parallel=parallel,
                                                 test=test))
        self.write("verdicts/QC-001.md", verdict("QC-001", "NEEDS-ENV", CHECKOUT + ":24"))
        self.qa("collect")
        return self.state()["findings"]["RC-001"]

    def test_race_confirmed_with_real_parallel_test_stays_confirmed(self):
        rel = ".qa-squad/%s/tests/OversellTest.php" % self.run_id()
        self.write("tests/OversellTest.php", self.PARALLEL_TEST)
        f = self.race_verdict(rel)
        self.assertEqual(f["verdict"], "CONFIRMED", f["verdict_note"])
        self.assertTrue(f["parallel_test"])
        self.assertEqual(f["test"], rel)
        self.assertEqual(self.state()["findings"]["QC-001"]["verdict"], "NEEDS-ENV")

    def test_race_test_path_may_be_relative_to_tests_dir(self):
        self.write("tests/OversellTest.php", self.PARALLEL_TEST)
        self.assertEqual(self.race_verdict("OversellTest.php")["verdict"], "CONFIRMED")

    def test_race_claimed_parallel_but_test_file_missing(self):
        f = self.race_verdict(".qa-squad/%s/tests/Nope.php" % self.run_id())
        self.assertEqual(f["verdict"], "LIKELY")
        self.assertIn("not found inside", f["verdict_note"])
        self.assertFalse(f["parallel_test"])

    def test_race_claimed_parallel_but_no_test_named(self):
        f = self.race_verdict("none")
        self.assertEqual(f["verdict"], "LIKELY")
        self.assertIn("no test file named", f["verdict_note"])

    def test_race_test_outside_run_tests_dir_does_not_count(self):
        outside = os.path.join(self.project, "tests", "Feature", "OversellTest.php")
        os.makedirs(os.path.dirname(outside), exist_ok=True)
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write(self.PARALLEL_TEST)
        f = self.race_verdict("tests/Feature/OversellTest.php")
        self.assertEqual(f["verdict"], "LIKELY")
        self.assertIn("not found inside", f["verdict_note"])

    def test_race_sequential_test_is_downgraded(self):
        self.write("tests/OversellTest.php", self.SEQUENTIAL_TEST)
        f = self.race_verdict("OversellTest.php")
        self.assertEqual(f["verdict"], "LIKELY")
        self.assertIn("uses no parallel construct", f["verdict_note"])

    def test_parallel_markers_across_stacks(self):
        for snippet in ("Process::pool(", "Concurrency::run([", "pcntl_fork()", "Http::pool(fn", "proc_open($cmd",
                        "from multiprocessing import Pool", "with ThreadPoolExecutor() as ex",
                        "await asyncio.gather(a(), b())", "await Promise.all([a(), b()])",
                        "require('child_process')", "Thread.new { buy }", "go func() {", "var wg sync.WaitGroup"):
            self.assertTrue(qa.PARALLEL_RE.search(snippet), snippet)
        for snippet in ("$this->postJson('/api/checkout');", "for i in range(2): client.post(url)",
                        "await a(); await b();"):
            self.assertFalse(qa.PARALLEL_RE.search(snippet), snippet)


# ---------------------------------------------------------------------------
# release gate
# ---------------------------------------------------------------------------

def gate_state(*findings, **kw):
    fs = {}
    for f in findings:
        f = dict(f, status="final")
        fs[f["id"]] = f
    return {"findings": fs, "deduped": True, "lanes": kw.get("lanes", ["functional", "race-conditions"]),
            "jobs": {"hunt": [], "verify": []}, "verify": kw.get("verify", True)}


def g(fid, sev, verdict_, lane="functional", category="logic", title="Something breaks", root="cause"):
    return mk(fid, lane, "app/X.php", 1, category, title, root, verdict_, sev)


class TestGate(unittest.TestCase):
    lanes = qa.load_lanes()

    def gate(self, *findings, **kw):
        return qa.compute_gate(gate_state(*findings, **kw), self.lanes)

    def test_block_on_confirmed_critical(self):
        r = self.gate(g("FN-001", "CRITICAL", "CONFIRMED"))
        self.assertEqual(r["result"], "BLOCK")
        self.assertIn("FN-001", r["reasons"][0])

    def test_block_on_likely_high_in_sensitive_lanes(self):
        for lane in ("race-conditions", "authz-security", "data-integrity"):
            self.assertEqual(self.gate(g("X-001", "HIGH", "LIKELY", lane=lane))["result"], "BLOCK", lane)
        self.assertEqual(self.gate(g("X-001", "HIGH", "LIKELY", category="authz"))["result"], "BLOCK")
        r = self.gate(g("X-001", "HIGH", "LIKELY", title="Refund applied twice"))
        self.assertEqual(r["result"], "BLOCK")
        self.assertIn("touches refund", r["reasons"][0])
        self.assertEqual(self.gate(g("X-001", "HIGH", "LIKELY", root="tenant scope bypassed"))["result"], "BLOCK")
        # a slow query in an invoicing app is not a billing-correctness bug
        self.assertEqual(self.gate(g("X-001", "HIGH", "CONFIRMED", category="perf",
                                     title="Invoice export query does a full table scan"))["result"], "WARN")

    def test_warn_cases(self):
        self.assertEqual(self.gate(g("X-001", "HIGH", "NEEDS-ENV", lane="race-conditions"))["result"], "WARN")
        self.assertEqual(self.gate(g("X-001", "HIGH", "CONFIRMED"))["result"], "WARN")
        self.assertEqual(self.gate(g("X-001", "MEDIUM", "LIKELY"))["result"], "WARN")
        self.assertEqual(self.gate(g("X-001", "CRITICAL", "UNVERIFIED"), verify=False)["result"], "WARN")

    def test_latent_warns_but_never_blocks(self):
        r = self.gate(g("FE-004", "HIGH", "LATENT", lane="frontend", category="authz",
                        title="Stored XSS via v-html, masked by the paginator crash"))
        self.assertEqual(r["result"], "WARN")
        self.assertIn("latent: masked by another bug", r["reasons"][0])
        self.assertEqual(self.gate(g("X-001", "LOW", "LATENT"))["result"], "PASS")
        self.assertEqual(qa.TESTCASE_STATUS["LATENT"], "Blocked")

    def test_pass_cases(self):
        self.assertEqual(self.gate()["result"], "PASS")
        self.assertEqual(self.gate(g("X-001", "LOW", "CONFIRMED"), g("X-002", "LOW", "LIKELY"))["result"], "PASS")
        self.assertEqual(self.gate(g("X-001", "CRITICAL", "DISPUTED"))["result"], "PASS")

    def test_gaps_listed(self):
        r = self.gate(g("X-001", "MEDIUM", "NEEDS-ENV"), lanes=["functional"])
        self.assertIn("ui-ux", r["gaps"]["skipped_lanes"])
        self.assertEqual(r["gaps"]["needs_env"], ["X-001: Something breaks"])
        self.assertEqual(len(r["gaps"]["uncovered"]), 10, "no hunter reported coverage for functional")


# ---------------------------------------------------------------------------
# resume and robustness
# ---------------------------------------------------------------------------

class TestResume(Base):
    def test_resume_mid_hunt_and_mid_verify(self):
        self.init("--lanes", "func,race", "--depth", "2", "--wave", "3")
        self.assertEqual(self.next()["argv"], ["prompts", "hunt"])
        self.qa("prompts", "hunt")
        first = self.next()
        self.assertEqual(first["action"], "spawn")
        self.assertEqual(len(first["calls"]), 3)
        # Crash after only two of the three agents in the wave finished.
        for c in first["calls"][:2]:
            standin.hunt(self.project, c["brief"])
        # Resume in a fresh process: next must offer exactly the unfinished jobs.
        proc = subprocess.run([sys.executable, os.path.join(SKILL, "qa.py"), "next", "--json",
                               "--project", self.project], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        again = json.loads(proc.stdout)
        done = {c["id"] for c in first["calls"][:2]}
        pending = [j["id"] for j in self.state()["jobs"]["hunt"] if j["id"] not in done]
        self.assertEqual([c["id"] for c in again["calls"]], pending)
        for c in again["calls"]:
            standin.hunt(self.project, c["brief"])
        self.assertEqual(self.next()["argv"], ["validate"])
        self.qa("validate")
        self.assertEqual(self.next()["argv"], ["prompts", "verify"])
        self.qa("prompts", "verify")

        v1 = self.next()
        self.assertEqual(v1["kind"], "verify")
        standin.verify(self.project, v1["calls"][0]["brief"])        # crash mid-verify
        v2 = self.next()
        all_ids = [j["id"] for j in self.state()["jobs"]["verify"]]
        self.assertEqual([c["id"] for c in v2["calls"]], all_ids[1:])
        for c in v2["calls"]:
            standin.verify(self.project, c["brief"])
        self.assertEqual(self.next()["argv"], ["collect"])
        self.qa("collect")
        self.assertEqual(self.next()["argv"], ["dedupe"])
        self.qa("dedupe")
        self.assertEqual(self.next()["argv"], ["report"])
        self.qa("report")
        self.assertEqual(self.next()["action"], "done")

    def test_silent_agent_gives_up_after_three_offers(self):
        self.init("--lanes", "func", "--depth", "1")
        self.qa("prompts", "hunt")
        for _ in range(qa.MAX_OFFERS):
            self.assertEqual(self.next()["action"], "spawn")
        self.assertEqual(self.next()["argv"], ["validate"])
        self.assertEqual(self.state()["jobs"]["hunt"][0]["status"], "failed")
        self.assertEqual(qa.compute_gate(self.state(), qa.load_lanes())["gaps"]["failed_jobs"], ["functional-1"])

    def test_mark_done_without_marker(self):
        self.init("--lanes", "func", "--depth", "1")
        self.qa("prompts", "hunt")
        self.next()
        self.qa("mark", "hunt", "functional-1")
        self.assertEqual(self.next()["argv"], ["validate"])


class TestAtomicWrites(Base):
    def test_crash_during_state_write_keeps_old_state(self):
        self.init("--lanes", "func", "--depth", "1")
        path = self.run_path("state.json")
        with open(path, "rb") as fh:
            before = fh.read()
        ctx = self.ctx()
        ctx.state["phase"] = "garbage"
        for target in ("os.replace", "os.fsync"):
            with mock.patch(target, side_effect=OSError("disk died")):
                with self.assertRaises(OSError):
                    ctx.save()
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), before, target)
            json.loads(before.decode("utf-8"))
            leftovers = [f for f in os.listdir(self.run_path()) if f.endswith(".part")]
            self.assertEqual(leftovers, [], "temp file must be cleaned up")

    def test_crash_mid_write_of_temp_file(self):
        self.init("--lanes", "func", "--depth", "1")
        path = self.run_path("state.json")
        with open(path, "rb") as fh:
            before = fh.read()
        real_fdopen = os.fdopen

        class Torn(object):
            def __init__(self, fh):
                self.fh = fh

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.fh.close()

            def write(self, data):
                self.fh.write(data[:len(data) // 2])
                raise KeyboardInterrupt("killed mid-write")

        with mock.patch("os.fdopen", side_effect=lambda fd, mode: Torn(real_fdopen(fd, mode))):
            with self.assertRaises(KeyboardInterrupt):
                qa.save_state(self.run_path(), {"phase": "x" * 10000})
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_leftover_temp_file_from_hard_kill_is_harmless(self):
        self.init("--lanes", "func", "--depth", "1")
        with open(self.run_path(".tmp-dead.part"), "w") as fh:
            fh.write('{"phase": "trunc')
        self.assertEqual(self.next()["argv"], ["prompts", "hunt"])


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------

class TestEndToEnd(Base):
    def test_nine_lanes_depth_two(self):
        dry_run.init_run(self.project, dry_run.NINE_LANES, 2)
        transcript, report_out = dry_run.drive(self.project)
        run_dir, st = dry_run.current_state(self.project)

        self.assertEqual(st["phase"], "reported")
        self.assertEqual(len([j for j in st["jobs"]["hunt"] if not j["fix"]]), 18)
        self.assertEqual([j["id"] for j in st["jobs"]["hunt"] if j["fix"]], ["adversary-fix"])
        self.assertEqual(sum(1 for f in st["findings"].values() if f["status"] == "rejected"), 3)
        self.assertEqual([j["id"] for j in st["jobs"]["verify"] if j["requeues"]], ["OP-001"])
        self.assertTrue(any(line.startswith("~ spawn 10 hunt") for line in transcript))
        self.assertTrue(all("wrote" in l or ": " in l for l in transcript if l.startswith("    ")))

        # the three planted bugs are flagged, and the gate blocks
        flagged = dry_run.planted_bugs_flagged(st)
        for name, ids in flagged.items():
            self.assertTrue(ids, name)
        self.assertEqual(st["gate"]["result"], "BLOCK")
        fs = st["findings"]
        self.assertEqual(fs["RC-001"]["found_by_lanes"], ["race-conditions", "data-integrity", "database"])
        self.assertEqual(fs["AZ-001"]["found_by_lanes"], ["authz-security", "adversary"])
        self.assertEqual(fs["RC-002"]["verdict"], "LIKELY")          # race CONFIRMED w/o parallel test
        self.assertEqual(fs["FN-002"]["verdict"], "DISPUTED")
        self.assertEqual(fs["PF-001"]["verdict"], "NEEDS-ENV")

        with open(os.path.join(run_dir, "REPORT.md"), encoding="utf-8") as fh:
            report = fh.read()
        for heading in ("# qa-squad report:", "## Release gate: BLOCK", "## Summary", "## Findings",
                        "### CRITICAL", "## Disputed", "## Needs environment", "## Coverage matrix",
                        "## Test cases"):
            self.assertIn(heading, report)
        for text in (st["run_id"], st["scope_sha256"], "| ID | Severity | Lane(s) | Title | Verdict | Location |",
                     "`multi-lane`", "**NOT COVERED**", "| ui-ux | 1 |", "skipped",
                     "app/Http/Requests/CheckoutRequest.php:18", "production-scale order counts",
                     "QA-CHECKOUT-001"):
            self.assertIn(text, report)
        self.assertNotIn("${", report)

        import csv
        with open(os.path.join(run_dir, "testcases.csv"), encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        with open(os.path.join(SKILL, "templates", "testcase.csv"), encoding="utf-8") as fh:
            header = fh.read().strip().split(",")
        self.assertEqual(rows[0], header)
        self.assertEqual(header, ["Test Case ID", "Feature", "Scenario", "Preconditions", "Test data", "Steps",
                                  "Expected result", "Actual result", "Priority/Severity", "Status"])
        status = {r[2].rsplit("(", 1)[1].rstrip(")"): r[9] for r in rows[1:]}
        self.assertEqual(status["AZ-001"], "Fail")
        self.assertEqual(status["RC-001"], "Blocked")
        self.assertEqual(status["PF-001"], "Not tested")
        self.assertEqual(status["FN-002"], "Pass")
        self.assertTrue(all(r[0].startswith("QA-CHECKOUT-") for r in rows[1:]))
        self.assertIn("GATE: BLOCK", report_out)

        # re-run one lane on the same scope
        self.qa("rerun", "race")
        st2 = self.state()
        self.assertNotEqual(st2["run_id"], st["run_id"])
        self.assertEqual(st2["scope_sha256"], st["scope_sha256"])
        self.assertEqual(st2["lanes"], ["race-conditions"])
        self.assertEqual(st2["flags"]["rerun_of"], st["run_id"])

    def test_no_verify_run(self):
        dry_run.init_run(self.project, None, None, extra=["--quick", "--no-verify"])
        transcript, report_out = dry_run.drive(self.project)
        run_dir, st = dry_run.current_state(self.project)
        self.assertFalse(any("verify" in line for line in transcript if line.startswith("~")))
        self.assertTrue(all(f["verdict"] == "UNVERIFIED" for f in st["findings"].values() if f["status"] == "final"))
        self.assertEqual(st["gate"]["result"], "WARN")
        with open(os.path.join(run_dir, "REPORT.md"), encoding="utf-8") as fh:
            self.assertIn("UNVERIFIED RUN", fh.read())


DOWNLOAD_CENTRE = os.path.join(HERE, "fixtures", "download-centre")


class TestFrontend(unittest.TestCase):
    def tmpdir(self):
        d = tempfile.mkdtemp(prefix="qa-squad-fe-")
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def write(self, root, rel, text):
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_detects_inertia_vue_fixture(self):
        fe = qa.detect_frontend(DOWNLOAD_CENTRE)
        self.assertEqual(fe["frameworks"], ["inertia", "vue"])
        self.assertEqual((fe["unit_runner"], fe["unit_command"]), ("vitest", "npx vitest run"))
        self.assertTrue(fe["testing_library"])
        self.assertFalse(fe["node_modules"])
        self.assertEqual(qa.detect_stack(DOWNLOAD_CENTRE)["stack"], "laravel")

    def test_detects_livewire_blade_react_and_none(self):
        d = self.tmpdir()
        self.write(d, "composer.json", '{"require": {"laravel/framework": "^11", "livewire/livewire": "^3"}}')
        self.write(d, "resources/views/orders/index.blade.php", "<div>{{ $order->id }}</div>")
        self.assertEqual(qa.detect_frontend(d)["frameworks"], ["livewire", "blade"])
        self.assertIn("Livewire::test()", qa.frontend_note(qa.detect_frontend(d)))

        r = self.tmpdir()
        self.write(r, "package.json", '{"dependencies": {"react": "18"}, "devDependencies": '
                                      '{"jest": "29", "@playwright/test": "1", "@testing-library/react": "14"}}')
        os.makedirs(os.path.join(r, "node_modules"))
        fe = qa.detect_frontend(r)
        self.assertEqual((fe["frameworks"], fe["unit_runner"], fe["e2e_runner"], fe["node_modules"]),
                         (["react"], "jest", "playwright", True))
        self.assertNotIn("NOT installed", qa.frontend_note(fe))

        self.assertIsNone(qa.detect_frontend(FIXTURE), "the checkout shop is API-only")

    def test_quick_adds_frontend_lane_only_when_project_has_one(self):
        code, out, _ = call(["plan", "--json", "--quick", "--project", DOWNLOAD_CENTRE])
        self.assertEqual(json.loads(out)["rows"][-1]["lane"], "frontend")
        code, out, _ = call(["plan", "--json", "--quick", "--skip", "fe", "--project", DOWNLOAD_CENTRE])
        self.assertNotIn("frontend", [r["lane"] for r in json.loads(out)["rows"]])
        code, out, _ = call(["plan", "--json", "--quick", "--project", FIXTURE])
        self.assertEqual(len(json.loads(out)["rows"]), 5)

    def test_lane_card_and_aliases(self):
        lanes = qa.load_lanes()
        for alias in ("frontend", "fe", "vue", "react", "livewire", "inertia", "js"):
            self.assertEqual(qa.resolve_lane(alias, lanes), "frontend", alias)
        self.assertEqual(qa.resolve_lane("ui", lanes), "ui-ux")
        card = qa.lane_map(lanes)["frontend"]
        self.assertEqual(card["prefix"], "FE")
        text = " ".join(card["checklist"])
        for topic in ("API contract", "out-of-order", "v-html", "Livewire", "Polling", "localStorage"):
            self.assertIn(topic, text)

    def test_frontend_brief_carries_stack_note_and_safety(self):
        tmp = self.tmpdir()
        project = os.path.join(tmp, "dc")
        shutil.copytree(DOWNLOAD_CENTRE, project)
        scope = os.path.join(tmp, "scope.md")
        with open(scope, "w", encoding="utf-8") as fh:
            fh.write("# Download centre\n\n## Frontend\nInertia + Vue page.\n")
        self.assertEqual(call(["init", "--project", project, "--task-file", scope, "--lanes", "fe",
                               "--depth", "1"])[0], 0)
        self.assertEqual(call(["prompts", "hunt", "--project", project])[0], 0)
        run_id = open(os.path.join(project, ".qa-squad", "CURRENT")).read().strip()
        with open(os.path.join(project, ".qa-squad", run_id, "briefs", "hunt", "frontend-1.md"),
                  encoding="utf-8") as fh:
            brief = fh.read()
        self.assertIn('lane "frontend"', brief)
        self.assertIn("Frontend: inertia, vue.", brief)
        self.assertIn("node_modules is NOT installed", brief)
        self.assertIn("7. Never install or update dependencies", brief)

    def test_gate_frontend_categories(self):
        lanes = qa.load_lanes()
        logic = g("FE-001", "HIGH", "CONFIRMED", lane="frontend", category="frontend",
                  title="Invoice list never renders: paginator treated as array")
        self.assertEqual(qa.compute_gate(gate_state(logic), lanes)["result"], "WARN")
        xss = g("FE-002", "HIGH", "LIKELY", lane="frontend", category="authz",
                title="Stored XSS via v-html on export label")
        self.assertEqual(qa.compute_gate(gate_state(xss), lanes)["result"], "BLOCK")


# ---------------------------------------------------------------------------
# performance & latency (bench.py)
# ---------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/slow":
            time.sleep(0.03)
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://8.8.8.8/")
            self.end_headers()
            return
        self.send_response(500 if self.path == "/err" else 200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *args):
        pass


class TestBench(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qa-squad-bench-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def bench(self, *argv):
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            code = bench.main(list(argv))
        return code, buf.getvalue(), err.getvalue()

    def url(self, path, host="127.0.0.1"):
        return "http://%s:%d%s" % (host, self.port, path)

    def result(self, name):
        with open(os.path.join(self.tmp, name), encoding="utf-8") as fh:
            return json.load(fh)

    def test_http_measurement_and_budget(self):
        out = os.path.join(self.tmp, "fast.json")
        code, text, err = self.bench("http", self.url("/fast"), "-n", "20", "-c", "2", "--budget-ms", "2000",
                                     "--label", "fast", "--out", out, "-H", "Authorization: Bearer s3cr3t")
        self.assertEqual(code, 0, err)
        r = self.result("fast.json")
        self.assertEqual((r["tool"], r["kind"], r["ok"], r["errors"], r["over_budget"]),
                         ("qa-squad-bench", "http", 20, 0, False))
        self.assertTrue(r["p50_ms"] <= r["p95_ms"] <= r["p99_ms"] <= r["max_ms"])
        with open(out, encoding="utf-8") as fh:
            self.assertNotIn("s3cr3t", fh.read(), "header values must never be stored")
        self.assertIn("p95=", text)
        self.assertEqual(self.bench("http", self.url("/slow"), "-n", "5", "--warmup", "0", "--budget-ms", "1",
                                    "--label", "slow", "--out", os.path.join(self.tmp, "slow.json"))[0], 0)
        self.assertTrue(self.result("slow.json")["over_budget"])

    def test_refuses_non_local_targets_and_big_runs(self):
        for url in ("http://8.8.8.8/", "http://10.0.0.1:80/", "http://example.com/", "ftp://127.0.0.1/"):
            code, _, err = self.bench("http", url, "-n", "3", "--label", "x",
                                      "--out", os.path.join(self.tmp, "x.json"))
            self.assertEqual(code, 2, url)
            self.assertIn("error", err)
        for args in (("-n", "501"), ("-c", "9"), ("--warmup", "50")):
            code, _, err = self.bench("http", self.url("/fast"), *(args + ("--label", "x", "--out",
                                                                            os.path.join(self.tmp, "x.json"))))
            self.assertEqual(code, 2, args)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "x.json")))

    def test_redirects_are_not_followed_and_errors_counted(self):
        self.bench("http", self.url("/redirect"), "-n", "3", "--warmup", "0", "--label", "r",
                   "--out", os.path.join(self.tmp, "r.json"))
        r = self.result("r.json")
        self.assertEqual((r["ok"], r["errors"], r["status_counts"]), (0, 3, {"302": 3}))
        self.bench("http", self.url("/err"), "-n", "2", "--warmup", "0", "--label", "e",
                   "--out", os.path.join(self.tmp, "e.json"))
        self.assertEqual(self.result("e.json")["errors"], 2)

    def test_localhost_is_pinned_to_the_listening_loopback_address(self):
        code, _, err = self.bench("http", self.url("/fast", host="localhost"), "-n", "5", "--label", "lh",
                                  "--out", os.path.join(self.tmp, "lh.json"))
        self.assertEqual(code, 0, err)
        r = self.result("lh.json")
        self.assertEqual(r["resolved_to"], "127.0.0.1")
        self.assertLess(r["p95_ms"], 1000, "no ~2s IPv6 fallback per request")
        code, _, err = self.bench("http", "http://localhost:1/", "-n", "1", "--label", "dead",
                                  "--out", os.path.join(self.tmp, "dead.json"))
        self.assertEqual(code, 2)
        self.assertIn("nothing is listening", err)

    def test_cmd_and_compare(self):
        code, _, err = self.bench("cmd", "-n", "2", "--warmup", "0", "--label", "py",
                                  "--out", os.path.join(self.tmp, "py.json"), "--", sys.executable, "-c", "pass")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.result("py.json")["ok"], 2)
        base = {"tool": "qa-squad-bench", "kind": "http", "p50_ms": 5}
        for name, p95 in (("s.json", 10.0), ("lin.json", 100.0), ("sup.json", 1000.0)):
            with open(os.path.join(self.tmp, name), "w") as fh:
                json.dump(dict(base, label=name, p95_ms=p95), fh)
        for large, expected in (("lin.json", "linear"), ("sup.json", "superlinear")):
            code, text, err = self.bench("compare", os.path.join(self.tmp, "s.json"), os.path.join(self.tmp, large),
                                         "--size-small", "1000", "--size-large", "10000",
                                         "--out", os.path.join(self.tmp, "cmp-" + large))
            self.assertEqual(code, 0, err)
            self.assertEqual(self.result("cmp-" + large)["scaling"], expected)


class TestAutoScaling(unittest.TestCase):
    def test_pairs_results_by_size_suffix(self):
        def r(label, p95, kind="http"):
            return {"label": label, "p95_ms": p95, "kind": kind}
        rows = {x["label"]: x for x in qa.auto_scaling([
            r("index-1k", 25.8), r("index-10k", 30.1),                 # flat
            r("job-10k", 86.5, "cmd"), r("job-100k", 630.2, "cmd"),    # linear
            r("pf002-prune-1k", 10.0, "cmd"), r("pf002-prune-10k", 400.0, "cmd"),  # superlinear
            r("orders", 12.0), r("single-5k", 9.0)])}                  # unpaired: ignored
        self.assertEqual(rows["index-1k vs index-10k"]["scaling"], "flat")
        self.assertEqual(rows["job-10k vs job-100k"]["scaling"], "linear")
        self.assertEqual(rows["pf002-prune-1k vs pf002-prune-10k"]["scaling"], "superlinear")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows["job-10k vs job-100k"]["size_growth"], 10.0)


class TestPerformanceRule(Base):
    def setUp(self):
        super(TestPerformanceRule, self).setUp()
        self.init("--lanes", "perf", "--depth", "1")
        self.qa("prompts", "hunt")
        self.write("findings/performance-1-1.md", good(
            lane="performance", category="perf", severity="MEDIUM", title="Order list issues one query per order",
            root_cause="product relation lazy-loaded in index",
            trigger="Seed 500 orders, then GET /api/orders and count the queries with DB::listen."))
        self.write("findings/performance-1.done", "findings: 1\ncovered: 1, 2, 3\n")
        self.qa("validate")
        self.qa("prompts", "verify")

    def verdict(self, measurement):
        text = standin.verdict_md("PF-001", "CONFIRMED", CHECKOUT + ":17", "Measured.").replace(
            "parallel_test: no", "parallel_test: no\nmeasurement: %s" % measurement)
        self.write("verdicts/PF-001.md", text)
        self.qa("collect")
        return self.state()["findings"]["PF-001"]

    def test_confirmed_perf_without_measurement_is_downgraded(self):
        f = self.verdict("none")
        self.assertEqual(f["verdict"], "LIKELY")
        self.assertIn("performance claims need a measurement file", f["verdict_note"])

    def test_confirmed_perf_with_measurement_file_stays_confirmed(self):
        os.makedirs(self.run_path("tests", "bench"))
        self.write("tests/bench/orders.json", json.dumps({"tool": "qa-squad-bench", "kind": "http"}))
        self.assertEqual(self.verdict("bench/orders.json")["verdict"], "CONFIRMED")

    def test_brief_and_report_section(self):
        with open(self.run_path("briefs", "hunt", "performance-1.md"), encoding="utf-8") as fh:
            brief = fh.read()
        self.assertIn("LATENCY MEASUREMENT", brief)
        self.assertIn(qa.BENCH_PATH, brief)
        self.assertIn("refuses non-localhost targets", brief)
        os.makedirs(self.run_path("tests", "bench"))
        self.write("tests/bench/orders-1k.json", json.dumps({
            "tool": "qa-squad-bench", "kind": "http", "label": "orders-1k",
            "target": "GET http://127.0.0.1:8099/api/orders", "requested": 50, "concurrency": 1, "ok": 50,
            "errors": 0, "p50_ms": 12.1, "p95_ms": 20.4, "p99_ms": 25.0, "budget_ms": 300, "over_budget": False}))
        self.write("tests/bench/orders-scaling.json", json.dumps({
            "tool": "qa-squad-bench", "kind": "compare", "label": "orders-1k vs orders-10k",
            "small": {"p95_ms": 20.4}, "large": {"p95_ms": 410.0}, "size_growth": 10.0, "p95_growth": 20.1,
            "exponent": 1.3, "scaling": "superlinear"}))
        self.verdict("bench/orders-1k.json")
        self.qa("dedupe")
        self.qa("report")
        with open(self.run_path("REPORT.md"), encoding="utf-8") as fh:
            report = fh.read()
        self.assertIn("## Performance measurements", report)
        self.assertIn("| orders-1k |", report)
        self.assertIn("**superlinear**", report)


class TestDetectAndCli(unittest.TestCase):
    def test_detect(self):
        self.assertEqual(qa.detect_stack(FIXTURE)["stack"], "laravel")
        self.assertEqual(qa.detect_stack(FIXTURE)["test_runner"], "pest")
        tmp = tempfile.mkdtemp()
        try:
            cases = {"package.json": ('{"dependencies": {"express": "4"}, "devDependencies": {"jest": "29"}}',
                                      "node:express"),
                     "pyproject.toml": ('[project]\ndependencies = ["django>=5"]\n', "django"),
                     "Gemfile": ('gem "rails"\n', "rails"),
                     "go.mod": ("module x\n", "go")}
            for name, (content, stack) in cases.items():
                d = os.path.join(tmp, name.replace(".", "_"))
                os.makedirs(d)
                with open(os.path.join(d, name), "w") as fh:
                    fh.write(content)
                self.assertEqual(qa.detect_stack(d)["stack"], stack, name)
            self.assertEqual(qa.detect_stack(tmp)["stack"], "unknown")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cli_entry_point(self):
        proc = subprocess.run([sys.executable, os.path.join(SKILL, "qa.py"), "plan", "--quick"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("sub-agent calls (expected): 30", proc.stdout)
        proc = subprocess.run([sys.executable, os.path.join(SKILL, "qa.py"), "next", "--project", tempfile.gettempdir(),
                               "--run", "does-not-exist"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("qa.py: error:", proc.stderr)

    def test_stdlib_only(self):
        import ast
        with open(os.path.join(SKILL, "qa.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        mods = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
        mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        allowed = {"argparse", "csv", "datetime", "difflib", "hashlib", "io", "json", "math", "os", "random",
                   "re", "shutil", "string", "sys", "tempfile"}
        self.assertTrue(mods <= allowed, mods - allowed)
        with open(os.path.join(SKILL, "bench.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        mods = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
        stdlib = {"argparse", "datetime", "ipaddress", "json", "math", "os", "socket", "subprocess", "sys",
                  "threading", "time", "urllib"}
        self.assertTrue(mods <= stdlib, mods - stdlib)


if __name__ == "__main__":
    unittest.main()
