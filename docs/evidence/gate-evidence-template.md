# Gate evidence record

Gate:

Status: `pass`, `fail`, `partial`, or `blocked`

Owner:

Captured at:

## Environment

Record tool versions, architecture, runtime boundaries, and whether the
environment is local disposable, CI, staging, or production.

## Immutable inputs

List source commits, release tags, OCI digests, configuration checksum, and
the exact app/source scope.

## Command

Record the reproducible command. Redact secrets rather than copying them.

## Result

Record exit code, concise output, and exact observed state. Keep build
completion, service health, runtime behavior, and business outcomes distinct.

## Checksums

List SHA-256 values for each retained compact artifact. Keep raw logs and large
generated artifacts outside Git with bounded retention.

## Interpretation

State only the conclusion directly supported by the result. Separate measured
fact from inference.

## Limitations and pending evidence

List warnings, unavailable capabilities, untested stages, owner/deadline, and
the command or event that will close each item.
