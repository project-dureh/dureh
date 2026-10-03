# Promoting checkpoints into derivatives

`checkpoints/` holds disposable pipeline outputs; `derivatives/<name>/` (a DataLad subdataset) holds
final results. Agents never write `derivatives/` directly: WI worktrees do not contain subdataset
content and project main trees are closed to agents. A dispatched `data-steward` promotes through
the harness:

```
dureh promote --wi <WI> --run <slurm run id> --source checkpoints/<step>/<path> --dataset <name> [--dest <path>]
```

The tool refuses unless all of these hold:

- the caller is `data-steward`, the WI is its own, dispatched, and bound to the calling agent;
- `--run` is a slurm run for the same project whose latest `run.status` is `COMPLETED`
  (`dureh slurm status --run <id>` records it);
- every source file resolves inside `checkpoints/` (symlinks that leave it are refused);
- every destination lies inside `derivatives/<name>/`, outside `.git`/`.datalad`, and does not exist yet
  (derivatives are final; promote to a new path);
- the subdataset is clean.

It clones the subdataset into `worktrees/<WI>-promote-<name>`, copies, runs `datalad save` with the
message `Promote <source> (WI <wi>, run <run>, job <job>)`, fast-forwards the subdataset from the clone,
fetches the annexed content, marks the clone dead and removes it, and saves the new subdataset state in
the project. Each promotion is recorded as a run record with `run_type: promotion`, the source, the
destinations, the run and the resulting commit. Promotions are serialized by the ledger lock.

Requires `datalad` and `git-annex` on the site's PATH (`config.json` `env.path_prepend`).

## Metadata files

Top-level metadata of a derivatives dataset (`dataset_description.json`, `README`, `README.md`,
`CHANGES`, `LICENSE`) is the one thing that may be replaced, because it describes the dataset rather
than being a result:

```
dureh promote --wi <WI> --dataset <name> --metadata dataset_description.json --input <file>
```

The input is a regular file in the data-steward's scratch space (`$TMPDIR`) or under `checkpoints/`;
JSON files must parse as an object. The save message is `Update <name> (WI <wi>)` (or `Add`), and the
run record notes whether a file was replaced. Promoting such a path with `--source` is refused.

## Generated reports

A report produced by code on the scheduler (for example camri + camri-vis: html, pdf, png) is final
output and follows BIDS: it lives inside the derivatives dataset, in a reports folder below it
(Director's decision, 2026-10-03, UR-20261003-02).

```
dureh promote --kind report --wi <WI> --run <slurm run id> --source checkpoints/<step>/<run>/report --dataset <name> --name <report name>
```

The report lands in `derivatives/<name>/reports/<report name>/` with the same checks as a promotion
(data-steward's own dispatched WI, a completed run, sources inside `checkpoints/`, a clean subdataset,
a new destination). The harness writes `provenance.json` next to it: work item, run and job id, the
revision of every site module, containers and inputs recorded in the run, and the sha256 of every file.
Reports are final; land the next one under a new name. Agents review and interpret reports
(`reports/interpretation/**`); they do not write them.
