"""UP-0069 (Director 2026-10-06), widened in release 46 (proposals/boundary-design-2026-10-08.md 1 and 8): the harness
carries no company or domain names. Harness code (`dureh_core/`), `org/`, `docs/`, the README and the tests name no
registered company, no domain data standard or layout word, and none of the roles release 46 removed from the
harness. A company's names are its startup package's (roles, labels, config_defaults) or a site's config values;
tests use the neutral example package (tests/fixtures/example-org). DataLad stays as a tool adapter (decision O2) and
is not a forbidden name.

Allowed: the explicit (file, substring) pairs below, each with its reason."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()
TERMS = {
    "company": r"camri",                                              # registered companies (upstream/exchange.json)
    "data standard": r"\bbids\b|each.from.bids|bids_",
    "domain layout": r"derivatives|sourcedata|rawbids",
    "domain tools and files": r"fmriprep|\.nii\b|\bT1w\b",
    "removed roles": r"pipeline-runner|data-steward|pm-projects|pm-harness|tool-writer|web-writer|[\"']interpreter[\"']",
}
PATTERN = re.compile("|".join(f"(?:{t})" for t in TERMS.values()), re.I)
# (path relative to the repository, substring of the one allowed line): why
ALLOWED = (
    ("tests/test_boundary.py", 'for gone in ("pipeline-runner", "data-steward", "interpreter:", "pm-projects"'),  # asserts the removal
    ("tests/test_boundary.py", '"tool-writer", "web-writer"):'),  # asserts the removal (continued line)
    ("tests/test_boundary.py", 'for gone in ("container build", "pipeline-runner", "data-steward"'),  # asserts the removal
)


def scanned() -> list[Path]:
    files = [ROOT / "README.md"]
    for base, globs in (("dureh_core", ("*.py",)), ("org", ("*",)), ("docs", ("*.md",)), ("tests", ("*.py", "*.yaml", "*.md", "*.template"))):
        for g in globs:
            files += [p for p in (ROOT / base).rglob(g) if p.is_file() and "__pycache__" not in p.parts]
    return sorted(set(p for p in files if p.resolve() != SELF))


class NoCompanyNames(unittest.TestCase):
    def test_no_company_or_domain_names(self):
        hits = []
        for path in scanned():
            rel = path.relative_to(ROOT).as_posix()
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for n, line in enumerate(lines, 1):
                if PATTERN.search(line) and not any(rel == f and sub in line for f, sub in ALLOWED):
                    hits.append(f"{rel}:{n}: {line.strip()[:160]}")
        self.assertEqual(hits, [], "company or domain names in the harness (move them to a package, config, or the "
                                   "neutral example; or add the line to ALLOWED with a reason):\n" + "\n".join(hits))

    def test_scan_covers_code_org_docs_and_tests(self):
        rels = {p.relative_to(ROOT).as_posix().split("/")[0] for p in scanned()}
        self.assertTrue({"dureh_core", "org", "docs", "tests", "README.md"} <= rels, rels)

    def test_allow_list_entries_still_exist(self):
        for rel, sub in ALLOWED:
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn(sub, text, f"{rel}: allow-list entry {sub!r} no longer matches; remove it")

    def test_the_pattern_catches_each_term(self):
        for sample in ("CAMRI", "--each-from-bids", "projects/p/derivatives/x", "fmriprep", "pipeline-runner", "'interpreter'"):
            self.assertTrue(PATTERN.search(sample), sample)
        for fine in ("python interpreter", "checkpoints", "datalad save", "subcommand"):
            self.assertFalse(PATTERN.search(fine), fine)
