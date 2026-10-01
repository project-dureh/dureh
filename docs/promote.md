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
Not covered yet: replacing metadata files that already exist in a subdataset, such as
`dataset_description.json`.
