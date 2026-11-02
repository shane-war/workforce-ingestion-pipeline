# Design notes: publish only validated snapshots

## Why full replacement?

A full snapshot has simple semantics: records absent from the new input disappear from the published dataset. That makes an unexpectedly small input dangerous. The default row-count guard requires explicit intervention when the dataset shrinks substantially.

## Transaction boundary

Parsing and semantic validation happen before acquiring the database write lock. Under `BEGIN IMMEDIATE`, the loader checks the current digest and row count, deletes the old snapshot, inserts the new rows, updates the digest, and records success. Failure rolls back those operations. A separate transaction records the failed attempt afterward.

The failure ledger cannot be guaranteed when the database itself is unavailable. File-read and GPG failures occur before `publish` and return a failed CLI status without a database run entry. This limitation is explicit rather than disguising every failure as a successful scheduled job.

## Encryption boundary

GPG supplies encryption processing. This code does not implement cryptography or ship keys. Decrypted bytes enter memory after a temporary-file read. Host access controls, key rotation, encrypted disks, and transport verification remain deployment responsibilities.

## Extension path

Add immutable arrival manifests and source sequencing; stage large batches in a separate table; add missing-ID and distribution checks; support controlled incremental changes; introduce metrics without logging employee attributes; qualify a database-specific promotion strategy through failure-injection tests.

## Interview discussion

Explain the difference between a job completing and a dataset being safely published. Walk through a failure on the second insert and show why the original snapshot remains available.
