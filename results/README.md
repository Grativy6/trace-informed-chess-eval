# Results

No live trial has been run in this repository state.

Each future trial directory must contain or point to:

- upstream commit;
- experiment commit;
- experiment-lock digest;
- exact model/provider identifier and parameters;
- task prompt hash;
- inspect log;
- verified TIAI JSONL trace;
- upstream grade metadata;
- classification review with supporting transcript spans;
- timestamps and available token/cost metadata.

Raw records are append-only. Corrections use sidecar notes or trace correction records; they do not rewrite the original run.
