"""T10's CODE half: the committed workflows must parse, cover both lanes +
the nightly chaos/fuzz lane, reference only make targets that exist, and
never arm live spend (FH_LIVE_SMOKE / eval-ab stay manual forever).

NB: yaml.safe_load parses a bare `on:` key as boolean True (YAML 1.1),
the repo's own known gotcha (docs/RETROFIT.md seam 1); tolerate both."""
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WF = REPO / ".github" / "workflows"


def _load(name):
    doc = yaml.safe_load((WF / name).read_text(encoding="utf-8"))
    doc["on"] = doc.get("on", doc.get(True))
    return doc


class TestWorkflows(unittest.TestCase):
    def test_ci_lanes(self):
        doc = _load("ci.yml")
        self.assertIn("push", doc["on"])
        jobs = doc["jobs"]
        self.assertIn("fast", jobs)
        self.assertIn("slow", jobs)
        pys = jobs["fast"]["strategy"]["matrix"]["python"]
        self.assertIn("3.10", pys)
        self.assertIn("3.13", pys)

    def test_slow_lane_fetches_tags_for_upgrade_path(self):
        doc = _load("ci.yml")
        checkout = next(s for s in doc["jobs"]["slow"]["steps"]
                        if "checkout" in s.get("uses", ""))
        self.assertEqual(checkout["with"]["fetch-depth"], 0,
                         "test_upgrade_path stamps the previous git tag")

    def test_nightly_runs_chaos_and_fuzz(self):
        doc = _load("nightly.yml")
        self.assertIn("schedule", doc["on"])
        runs = "\n".join(step.get("run", "") for j in doc["jobs"].values()
                         for step in j["steps"])
        self.assertIn("make chaos", runs)
        self.assertIn("make fuzz", runs)

    def test_only_real_make_targets_and_no_live_spend(self):
        mk = (REPO / "Makefile").read_text(encoding="utf-8")
        targets = {ln.split(":")[0] for ln in mk.splitlines()
                   if ln and not ln.startswith((".", "\t", "#")) and ":" in ln}
        for name in ("ci.yml", "nightly.yml"):
            text = (WF / name).read_text(encoding="utf-8")
            self.assertNotIn("FH_LIVE_SMOKE", text)
            self.assertNotIn("eval-ab", text)
            for line in text.splitlines():
                line = line.strip().removeprefix("- ")
                if line.startswith("run: make "):
                    for tgt in line.removeprefix("run: make ").split():
                        self.assertIn(tgt, targets,
                                      f"{name}: unknown make target {tgt}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
