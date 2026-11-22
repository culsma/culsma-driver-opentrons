# Culsma driver for Opentrons

This driver connects the Culsma CLI to the Opentrons Python simulator. Pipetting and supported plate movements run through the official simulator. Reference device models supply centrifugation and incubation for the [whole-blood automation benchmark](https://github.com/culsma/culsma/tree/v1.0.11/benchmarks/automation/whole-blood-lps-rbc). The benchmark records simulated actions and device feedback. Use this distribution for software simulation.

The repository is named `culsma-driver-opentrons`. The Python distribution used for the benchmark is `culsma-opentrons-simulator` 0.1.0 and its CLI driver name is `opentrons-simulator`.

## Install and test

From this checkout, create a Python 3.11 environment and install the Culsma core used for the automation evaluation and the driver with its test dependencies.

```sh
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install 'culsma==1.0.11' 'lark==1.3.1' '.[dev]'
python -m pytest -q
```

For the paper, the [benchmark README](https://github.com/culsma/culsma/blob/v1.0.11/benchmarks/automation/README.md) gives the program, deck configuration, dependency installation instructions, a shared reproduction command and checks on the resulting records. The [examples](examples/) provide smaller inputs and workstation configurations. The [standalone tests](tests/standalone/) exercise driver registration, device requests, capability checks and execution feedback.

Distributed under the [Apache License 2.0](LICENSE).
