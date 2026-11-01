# Portable DAF adapters with DJcode

`djcode.adapter_workflow.execute_adapter_workflow` schedules Python or Rust DAF adapter requests through DJcode's existing Rust graph engine and real DDAL transport. Applications supply an approval callback and an adapter runner. Denied requests never reach the runner, and failed commands block dependent nodes. The bridge does not automatically add a model tool or replace `DJCODE_DAF_ENGINE`.

```python
from daf_adapter.runner import run
from djcode.adapter_workflow import execute_adapter_workflow

nodes = [{
    "id": "inspect",
    "name": "daf_adapter_run",
    "arguments": {
        "schema_version": 1,
        "id": "inspect",
        "argv": ["git", "status", "--short"],
        "timeout_ms": 5000,
    },
}]

async def approve(name, request):
    # Connect this to your session's actual approval UI or explicit policy.
    return await session._approve_tool(name, request)

results = await execute_adapter_workflow(nodes, approve, run)
```

The example assumes an existing `session`, asynchronous application context and the separate `daf_adapter` Python package installed. For Rust execution, supply an async runner that invokes the standalone adapter binary with a JSON request and decodes its JSON result. Keep approval outside the runner and before subprocess execution. Command arguments are literal; there is no shell interpolation or automatic retry.

Each request ID must match its workflow node ID. Results retain structured status, exit code, output and truncation indicators. A denied or dependency-skipped node returns an error object. `concurrency` is 1..4. DJcode's additional 1 MiB reply limit applies to serialized results: large output or JSON escaping may exceed it even if the process adapter's capture cap admits the raw bytes. Choose a smaller capture cap when needed. The adapter inherits environment and filesystem access and is not a sandbox.

The acceptance script `scripts/check_adapter_integration.py` exercises real DDAL transport, dependency success and failure, and denial with an isolated temporary configuration directory. It requires `daf_adapter` and DJcode on `PYTHONPATH`, plus `DJCODE_DAF_ENGINE` pointing to the actual bundled Rust engine binary. No provider credentials or live inference are required.
