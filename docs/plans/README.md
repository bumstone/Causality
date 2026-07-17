# Delivery Plans

Live delivery order lives here. ADRs explain decisions; the status dashboard
summarizes progress; specs define implementation contracts.

See the [implementation-spec index](../specs/README.md) for phase contracts.

| Order | Plan | Status |
| --- | --- | --- |
| 001 | [External harness delivery](001-external-harness-delivery.md) | implemented |
| 002 | [Automatic orchestration](002-automatic-orchestration.md) | planned |

Rules:

- Implement numbered phases in order unless a plan explicitly permits parallel work.
- Do not mark a phase complete without its spec acceptance tests and ledger evidence.
- Update the dashboard and this table when a phase changes status.
