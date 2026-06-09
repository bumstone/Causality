# Third-Party Notices

This repository is an original implementation that combines ideas from several
MIT-licensed agent workflow projects. It does not vendor source code from those
projects.

The following projects influenced the architecture and terminology:

| Project | Upstream | License | How it is used here |
| --- | --- | --- | --- |
| Ouroboros | https://github.com/Q00/ouroboros | MIT | Architectural reference for Seed, Ledger, Runtime, MCP, and specification-first workflows. |
| Ouroboros Plugins | https://github.com/Q00/ouroboros-plugins | MIT | Reference model for plugin contracts, permissions, audit events, and user-level workflow packages. |
| Superpowers | https://github.com/obra/superpowers | MIT | Workflow inspiration for planning, TDD, root-cause debugging, verification before completion, and slash-command ergonomics. |
| gstack | https://github.com/garrytan/gstack | MIT | Browser workflow inspiration for compact A11y snapshots, ref-based actions, diffs, and evidence-oriented QA loops. |

## Private Repository Guidance

MIT-licensed projects generally allow private modification, internal use,
redistribution, sublicensing, and publication, provided the copyright and
permission notices are preserved when substantial portions of the upstream
software are copied or distributed.

This repository preserves attribution in this notice even though it is an
original implementation. Keep this file and `LICENSE` when pushing to a private
GitHub repository or when later sharing the project.

This file is a practical engineering note, not legal advice.
