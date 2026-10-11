# Evidence-driven quality and automatic repairs

The `ash-quality` host worker runs separately from conversation execution. It reads
Braintrust snapshots and Sentry incidents, records decisions durably, proposes source
repairs, validates them in isolation, and optionally commits and pushes reviewed fixes
to `main`. Pushing is not deployment or proof that a production incident recovered.
The worker never resolves Sentry issues or changes the running application's checkout.

## Evidence and diagnosis

Braintrust snapshots v2 separate the current turn from retained history, including
boundary/truncation markers. Missing, truncated, old-format and synthetic records cannot
trigger semantic repairs. The judge evaluates grounding, instruction following and honest
completion against visible evidence. Unknown means unknown; tool failure alone is not an
inaccurate reply. Failures require verbatim evidence, including current-turn evidence.
Scores and repair status merge into the original Braintrust row and are read back before
publication is marked complete. Sentry receives linked high-confidence quality findings.

Sentry intake reads unresolved incidents from the configured organization/project, sorted
by frequency with a durable pagination cursor. Only titles, exception descriptions, source
frame locations, release and occurrence metadata enter diagnosis, after scrubbing. Request
bodies, locals, breadcrumbs and user objects are excluded. Smoke-test events and the
worker's own quality events cannot feed repairs. Issue + release deduplication bounds repeat
work. The latest event remains attached as provenance. A new release can generate a new case.
Incident quotes must match original string values, not JSON serialization or generated
field labels; the prompt specifies that format explicitly.
The incident judge distinguishes code defects from configuration problems, provider/network
failures and insufficient evidence; only high-confidence code defects enter repair.

Redacted evidence and relevant source code are sent to the configured OpenAI models for
scoring, diagnosis, proposal and independent review. This must be authorized for the deployment.
Credentials are never supplied to models. Data minimization and scrubbing are best effort.
A source outage is recorded in status and does not block the other source or queued work.

## Durable worker and budgets

SQLite tracks evidence identity + rubric version, judgments, retries, publication and repair
state. One process lock prevents overlapping runs. Daily judge, candidate and review limits
are reserved before calls and survive restarts. Candidate generation allows at most eight
model calls; review uses one separate request. Limits bound requests, not dollar spending.
Interrupted candidate generation is quarantined; interrupted pushes resume the same commit.
Unknown/error judgments cannot trigger repairs. No model has shell or push credentials.

## Validation and review

Automatic mode fetches a pinned remote `main` revision and exports its source. Older or
unversioned evidence must reproduce on this current baseline; an old release alone is never
proof of a current defect. The original evidence release is retained, not rewritten.
The model has read-only source/search tools and returns exact replacements in tracked
`src/ash/` files, excluding configuration, monitoring and the repair machinery. One uniquely
named regression test is added per candidate, so multiple successful repairs can accumulate.

Generated code runs only inside Bubblewrap, without network, credentials or host home mounts.
Runtime, dependencies and candidate source are read-only. The frozen regression must collect
one test and fail by AssertionError on baseline, then pass after the patch. The original unit
suite runs separately, excluding the generated test so its module cannot alter existing tests.
The changed files must pass Ruff lint and formatting checks. Test hashes, file hashes, patch,
base revision and reports are preserved in a private artifact directory.

A fresh, tool-free model review sees the original evidence, diff and changed files. It must
agree that the patch addresses the evidence, the test meaningfully reproduces it, and safety
and existing behavior are preserved. Error suppression, test/report manipulation, weakened
permissions and unrelated changes must be rejected. Tests and model review reduce risk; they
do not guarantee semantic correctness. Ambiguous fixes stay quarantined.

## Main promotion

`auto_push = true` additionally requires an exact `push_url` matching both fetch and push
URLs of `push_remote` (default `origin`). The destination branch is fixed to `main`.
Promotion rechecks hashes against both validation and independent review. Trusted Git
plumbing uses a private index to create a commit without checking out or importing generated
code, invoking Git hooks, or changing the live checkout. Commit messages contain case/base
identifiers and AI attribution, not conversation text.

The remote head must equal the tested base. Push is a normal fast-forward push, never forced.
A concurrent writer causes rejection; the worker does not rebase an untested patch. The exact
commit is durably recorded before push, and remote ancestry confirms success. If the push
response is lost, subsequent polls recover that same commit rather than creating another.
Stale bases and failed/rejected reviews require an explicit retry with fresh validation.

## Operations

```toml
[quality]
enabled = true
repair_enabled = true
auto_push = true
push_remote = "origin"
push_url = "https://github.com/OWNER/REPO.git" # must match the configured remote exactly
sentry_org = "YOUR_ORG"
sentry_project = "YOUR_PROJECT"
judge_model = "gpt-5.2"
repair_model = "gpt-5.2"
review_model = "gpt-5.2"
max_scores_per_day = 100
max_repairs_per_day = 1
poll_seconds = 120
```

Provide `SENTRY_AUTH_TOKEN` via a private service environment file; Braintrust and model
credentials use the existing configuration. `once` processes a batch and at most one repair;
`serve` repeats; `status` shows aggregate work and source failures without transcripts.
`retry CASE_ID` archives failed artifacts and requeues a candidate. `repair CASE_ID` builds
one eligible candidate; automatic promotion is performed by `once`/`serve`.
Stop the quality worker or disable `auto_push` to pause promotion; Ash remains operational.

Terminal states distinguish insufficient_evidence, judge_error, needs_reproduction,
validation_failed, interrupted, review_failed, review_rejected, stale_base and pushed.
`review_ready` and `push_pending` are resumable stages. `pushed` does not mean deployed.
