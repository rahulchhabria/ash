# Conversation quality and repair candidates

The `ash-quality` host worker is separate from conversation execution. It polls
Braintrust, stores work durably in SQLite, scores retained evidence, updates the
original Braintrust row, and creates bounded repair candidates. It never executes
conversation instructions, changes the live checkout, deploys, or resolves incidents.

## Evidence and scores

Snapshots v2 include the current turn separately from retained history, with explicit
boundary/truncation markers. Missing, truncated, old-format and synthetic records are
not eligible for automatic repair. A tool error is an operational observation, not
proof of an inaccurate reply. Model judgments evaluate instruction following,
grounding and honest task completion against visible evidence only. Scores may be
unknown. Failures require a concrete verbatim evidence quote; unsupported quotes
invalidate the judgment. Judges receive data as untrusted user content and have no
tools. Judge model, rubric version, confidence and reasons are stored with scores.

## Durable worker

SQLite tracks record ID + rubric version, score, retries, publication and repair
state. A single process lock prevents overlapping runs; interrupted scoring retries,
while interrupted repair attempts need review and are not automatically repeated.
Daily judge-attempt and repair-candidate limits are reserved before API calls and survive
restarts; each candidate permits at most eight model requests. Retry limits and a bounded per-poll batch prevent loops. Published score
updates merge into existing Braintrust rows. Sentry receives high-confidence
findings with stable fingerprints and Braintrust/case identifiers, not transcripts.
Unknown/error judgments cannot trigger repairs. Budgets bound requests, not dollars.

## Repair pipeline

A high-confidence explicit failure with complete current-turn evidence is eligible.
The repair model may read only tracked source/tests/specs using constrained read-only
functions. It has no shell, network, credentials, deployment or write tools. It
returns validated exact-text edits to `src/ash/` (excluding this worker and monitoring
code) plus one regression test. Generated code is never executed on the host.

The runner exports a pinned commit to a private candidate directory, adds the test,
and uses Bubblewrap with no network, no home/vault mounts, a read-only Python
runtime/venv, read-only candidate source and an ephemeral home to execute tests. The initial regression must
collect exactly one test and fail by AssertionError, not import/setup errors. The
same frozen test must then pass after applying source edits. The original unit suite
must also pass. Reports, diff, source revision, test hashes and evidence are retained.
The test and patch are model proposals, not independent proof of behavioral accuracy;
passing candidates require review and relevant behavioral evals before deployment.
The worker never imports modified quality code or accepts model-provided commands.

Failure states include insufficient_evidence, judge_error, needs_reproduction,
validation_failed, interrupted, and review_ready. No zero-test, skipped-test, changed
harness, stale-base or judge-error result can be interpreted as verified recovery.

## Operations

`ash-quality once` collects and scores a bounded batch and attempts at most one
eligible repair when enabled. `serve` repeats with a configured interval. `status`
prints counts without conversation content. `retry CASE_ID` explicitly requeues a
failed repair only; `repair CASE_ID` runs a single eligible pending candidate. No
command merges or deploys generated patches. Stop the worker to pause automation;
Ash itself remains operational. Restrictive state-directory permissions are required.
