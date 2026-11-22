# Driver examples

These standalone examples demonstrate pipetting configuration, material records,
and capability checks. Each case directory contains `protocol.culs`, `deck.json`,
and an `expected.json` file describing the expected result.

| Example | Demonstrates |
| --- | --- |
| `multi-source` | Mixing multiple sources and tracking their components |
| `low-volume-recipe` | Low-volume transfers, multiple sources, and configured flow rates |
| `serial-dilution` | Material and component accounting through serial dilution |
| `split-transfer` | Splitting a transfer into strokes within the pipette range while preserving the requested total volume |
| `unsupported-separation` | Rejecting a magnetic separation request unsupported by the configuration |

## Run an example

Follow the [installation instructions](../README.md#install-and-test), then run
from the repository root:

```sh
.venv/bin/culsma run examples/low-volume-recipe/protocol.culs \
  --driver opentrons-simulator \
  --driver-config examples/low-volume-recipe/deck.json \
  --artifacts-dir run-artifacts/low-volume-recipe
```

Replace the example directory to run another case. Inspect the execution status
and device receipts in the output directory's `run.json`, and compare them with
that case's `expected.json`. The expected outcome of `unsupported-separation` is
a capability-check rejection.

## Complete workflow

The [whole-blood automation benchmark](https://github.com/culsma/culsma/tree/main/benchmarks/automation)
provides a complete workflow and a single reproduction command. It demonstrates
configured `high_precision` pipetting, `low_carryover` tip handling, plate movement,
centrifugation, and temperature and CO₂ holds. Pipetting and supported plate
movements use the official Opentrons simulator; centrifugation and environmental
holds use reference device models.
