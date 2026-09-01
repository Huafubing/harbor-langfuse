# harbor-langfuse

Langfuse plugin for Harbor jobs.

```bash
export LANGFUSE_HOST="http://localhost:3000"
export LANGFUSE_PUBLIC_KEY="pk-lf-..."
export LANGFUSE_SECRET_KEY="sk-lf-..."

harbor run ... --plugin langfuse
```

What it does per job:

- **Dataset sync** (optional): Harbor tasks are upserted into a Langfuse dataset
  (inputs = task name + instruction), enabling experiment comparison in the UI.
- **Experiment mapping**: the job becomes one Langfuse experiment; each trial's
  ATIF trajectory is converted to OpenTelemetry spans (via
  [harbor-atif2otel](../harbor-atif2otel)) and enriched with `langfuse.*`
  attributes so all traces group under the experiment.
- **Score backfill**: `TrialResult.verifier_result.rewards` → Langfuse Scores
  (`NUMERIC`); trials that ended with an exception get a boolean
  `harbor_error` score. Scores are upserted by deterministic ids, so re-running
  is idempotent.

## Options

| Option | Environment variable | Default |
| --- | --- | --- |
| `host` | `LANGFUSE_HOST` | `http://localhost:3000` |
| `public_key` | `LANGFUSE_PUBLIC_KEY` | — |
| `secret_key` | `LANGFUSE_SECRET_KEY` | — |
| `dataset_name` | `HARBOR_LANGFUSE_DATASET` | `harbor-{job_name}` |
| `experiment_name` | `HARBOR_LANGFUSE_EXPERIMENT` | `{job_name}-{job_id[:8]}` |
| `release` | `HARBOR_LANGFUSE_RELEASE` | — |
| `sync_dataset` | `HARBOR_LANGFUSE_SYNC_DATASET` | `true` |
| `export_traces` | `HARBOR_LANGFUSE_EXPORT_TRACES` | `true` |
| `fail_fast` | `HARBOR_LANGFUSE_FAIL_FAST` | `false` |

`export_traces` requires the [`harbor-atif2otel`](../harbor-atif2otel) package;
without it the plugin falls back to scores-only mode.

## Data residency

All traffic targets the configured `LANGFUSE_HOST` — typically a self-hosted
instance. Nothing is sent to any third party. Run `--plugin langfuse` with
`--plugin atif2otel --pk output_dir=...` if you need offline batch export
instead of streaming.
