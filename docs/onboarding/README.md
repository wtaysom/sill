# Onboarding

Start with a Sill memory you can retrieve in a different session. Use a
synthetic preference, save the UUID returned by `remember`, and confirm
that `recall` returns the same content and UUID in a fresh session. Try a
different connected client if you have one. The
[first-use walkthrough](01-install.md) includes both prompts.

| Phase | Done when |
|---|---|
| 1. Install Sill | `./verify.sh` passes for the local runtime |
| 2. Connect and use Sill | Your client lists the tools and a fresh session returns the original memory and UUID |

A person can follow this walkthrough in a terminal, or ask their agent to
guide them while they handle app sign-ins and permission prompts. If a step
fails, use its diagnostic before continuing.

## Optional setup after the memory round trip

You can stop after the round trip and keep using Sill for deliberate memory.
Choose an identity and charter if you want to define an ongoing project for
the instance. Backfill needs its own consent. A schedule is for an instance
you want to run when you are away.

| Phase | Guide | Done when |
|---|---|---|
| 3. Identity card | [Install and first use](01-install.md#optional-identity-and-worker-permissions) | `sill identity show` prints the card |
| 4. **Permissions verified** for unattended work | [Worker permissions](01-install.md#worker-permissions) | A headless run creates a file in this directory |
| 5. Consent-scoped backfill | [Backfill](02-backfill.md) | You reviewed the plan and confirmed the run, or skipped it |
| 6. Supervised first beats | [First beats](03-first-beats.md) | Each selected voice completed and you read its output |
| 7. Seeded-fault drill | [Guard drill](04-seeded-fault-drill.md) | A wired guard refused a fabricated receipt |
| 8. Christening | [Charter and name](05-christening.md) | You chose a name and wrote a charter in your own words |
| 9. Schedule | [Scheduling](../../scheduling/README.md) | You chose an interval after verifying worker output and the guard drill |

A headless CLI can report success in prose after permission to write was
denied. Check the actual file before starting a schedule. The guard drill
requires the Claude Code or Codex CLI hooks for the project.

## Reading the letter

The [onboarding letter](../../onboarding/to-the-one-who-wakes-here.md) is
addressed to the instance. Read it at the christening, again after the first
live guard refusal, and once more at the first tending review, when a person
has read a stretch of entries and can say what they found.
