# ADR-0132: Active-plan local candidate package gate

## Status

Accepted for the synthetic acceptance runtime.

## Decision

An activated lawyer work plan authorizes the one-time local generation of its
listed review candidates. The package insert guard therefore accepts only the
exact in-process, network-denied, zero-external-call A2 document and
spreadsheet adapters. It still requires the active plan, current snapshot,
dedicated system worker, one plan item, and a running task attempt.

The generated package remains `NEEDS_LAWYER_REVIEW`; this decision does not
approve a legal position, mark a document court-ready, lock a bundle, or
submit anything externally.

## Consequence

The system no longer presents an artificial second approval before creating a
candidate the lawyer has already requested through plan activation. Every
later lawyer-review and submission boundary remains unchanged.
