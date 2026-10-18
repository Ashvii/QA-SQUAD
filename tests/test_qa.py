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
        self.assertEqual((p["lanes"], p["hunters"], p["skeptics"], p["total"]), (5, 5, 15, 20))
        self.assertEqual((p["hunt_waves"], p["verify_waves"], p["waves"]), (1, 2, 3))
        self.assertFalse(p["needs_consent"])

    def test_default_all_lanes(self):
        p = self.plan()
        self.assertEqual((p["lanes"], p["hunters"], p["skeptics"], p["total"]), (10, 20, 60, 80))
        self.assertEqual(p["waves"], 2 + 6)
        self.assertTrue(p["needs_consent"])

    def test_aliases_and_depth(self):
        p = self.plan("--lanes", "race,authz,queue,db,perf", "--depth", "3")
        self.assertEqual([r["lane"] for r in p["rows"]],
                         ["race-conditions", "authz-security", "database", "queue-cache-external", "performance"])
        self.assertEqual((p["hunters"], p["skeptics"], p["total"]), (15, 45, 60))
        self.assertFalse(p["needs_consent"], "exactly 60 calls does not need consent")
        self.assertEqual((p["hunt_waves"], p["verify_waves"]), (2, 5))

    def test_no_verify_and_skip_and_estimate(self):
        p = self.plan("--skip", "ui-ux", "--depth", "1", "--no-verify")
        self.assertEqual((p["lanes"], p["hunters"], p["skeptics"], p["total"], p["waves"]), (9, 9, 0, 9, 1))
        p = self.plan("--skip", "ui,perf", "--est-findings", "2")
        self.assertEqual((p["lanes"], p["hunters"], p["skeptics"], p["total"]), (8, 16, 32, 48))

    def test_wave_size(self):
        p = self.plan("--quick", "--wave", "2")
        self.assertEqual((p["hunt_waves"], p["verify_waves"]), (3, 8))

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
        self.assertIn("sub-agent calls (expected): 20 = 5 hunters + 15 skeptics", out)
        self.assertIn("waves: 3 (1 hunt + 2 verify", out)


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
            mk("DB-001", "database", C, 31, "race", "No row lock", "missing lockForUpdate", "LIKELY", "MEDIUM"),
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
        b = mk("B", "y", "f.php", 20, "race", "Other", "x", "LIKELY", "LOW")
        self.assertEqual(qa.same_issue(a, b, same_fn), "same-function")
        self.assertIsNone(qa.same_issue(a, b, lambda f: "store" if f["id"] == "A" else "update"),
                          "nearby but in different functions")
        self.assertIsNone(qa.same_issue(a, b, unknown_fn), "unknown function and dissimilar wording")
        c = mk("C", "y", "f.php", 20, "race", "Title one again", "root one, twice", "LIKELY", "LOW")
        self.assertEqual(qa.same_issue(a, c, unknown_fn), "same-file-nearby", "unknown function, similar wording")
        self.assertIsNone(qa.same_issue(a, mk("B", "y", "f.php", 21, "race", "Other", "x", "LIKELY", "LOW"), same_fn))
        self.assertIsNone(qa.same_issue(a, mk("B", "y", "f.php", 12, "authz", "Other", "x", "LIKELY", "LOW"), same_fn))

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
            mk("AZ-001", "authz-security", T, 11, "authz", "Update loads any team", "no ownership check",
               "LIKELY", "HIGH"),
            mk("AD-001", "adversary", T, 12, "authz", "Editing someone else's team", "missing policy on update",
               "LIKELY", "MEDIUM"),
        ])
        self.assertEqual(fs["AZ-001"]["merged_from"], ["AD-001"])
        self.assertEqual(self.state()["dedupe_rules"], ["AD-001 ~ AZ-001 (same-function)"])

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

    def test_warn_cases(self):
        self.assertEqual(self.gate(g("X-001", "HIGH", "NEEDS-ENV", lane="race-conditions"))["result"], "WARN")
        self.assertEqual(self.gate(g("X-001", "HIGH", "CONFIRMED"))["result"], "WARN")
        self.assertEqual(self.gate(g("X-001", "MEDIUM", "LIKELY"))["result"], "WARN")
        self.assertEqual(self.gate(g("X-001", "CRITICAL", "UNVERIFIED"), verify=False)["result"], "WARN")

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
        self.assertIn("sub-agent calls (expected): 20", proc.stdout)
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


if __name__ == "__main__":
    unittest.main()
