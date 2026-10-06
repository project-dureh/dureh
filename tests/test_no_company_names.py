"""UP-0069 (Director 2026-10-06): the harness carries no company's module, manifest or variable names in code. A
string that names one is a config value (project.*, containers.*, modules[].*, slurm.each_env) or an example labelled
as such. This test greps dureh_core/ for `camri` (any case) outside a minimal, explicit allow-list of help-text examples."""
import re
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent / "dureh_core"
PATTERN = re.compile(r"camri", re.I)
# (file relative to dureh_core/, substring of the one allowed line): help-text examples labelled as a company's names
ALLOWED = (
    ("ops.py", 'the company\'s own names, e.g. CAMRI_SUBJECT'),          # item_env docstring: slurm.each_env example
    ("runtimes/claude_code.py", "for example `camri run <pipeline> <step>"),  # desk guidance item 8: the --via example
)


class NoCompanyNames(unittest.TestCase):
    def test_core_names_no_company(self):
        hits = []
        for path in sorted(list(CORE.glob("*.py")) + list((CORE / "runtimes").glob("*.py"))):
            rel = path.relative_to(CORE).as_posix()
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if PATTERN.search(line) and not any(rel == f and sub in line for f, sub in ALLOWED):
                    hits.append(f"{rel}:{n}: {line.strip()}")
        self.assertEqual(hits, [], "company names in harness code (move them to config or label them as an example "
                                   "and add the line to ALLOWED):\n" + "\n".join(hits))

    def test_allow_list_entries_still_exist(self):
        for rel, sub in ALLOWED:
            text = (CORE / rel).read_text(encoding="utf-8")
            self.assertIn(sub, text, f"{rel}: allow-list entry {sub!r} no longer matches; remove it")
