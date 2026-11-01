"""Approval and result-integrity regression tests for the portable adapter bridge."""

import unittest
from unittest.mock import patch

from djcode.adapter_workflow import execute_adapter_workflow


def node(ident="one"):
    return {
        "id": ident,
        "name": "daf_adapter_run",
        "arguments": {"schema_version": 1, "id": ident, "argv": ["example"]},
    }


def result(ident="one", status="succeeded"):
    return {
        "schema_version": 1,
        "id": ident,
        "status": status,
        "exit_code": 0 if status == "succeeded" else 2,
        "stdout": "ok",
        "stderr": "",
        "stdout_truncated": False,
        "stderr_truncated": False,
    }


class AdapterWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, approve, run, nodes=None):
        async def execute(engine, items, dispatch, concurrency):
            self.assertEqual(engine.mode, "daf")
            self.assertEqual(concurrency, 1)
            return {n["id"]: await dispatch(n["name"], n["arguments"]) for n in items}

        with patch("djcode.adapter_workflow.WorkflowEngine.execute", execute):
            return await execute_adapter_workflow(nodes or [node()], approve, run)

    async def test_denial_never_calls_runner(self):
        async def approve(*args):
            return False

        async def run(request):
            self.fail("denied adapter was executed")

        outcome = await self.exercise(approve, run)
        self.assertIn("denied", outcome["one"]["error"])

    async def test_approval_mutation_cannot_change_request(self):
        async def approve(name, request):
            request["argv"] = ["mutated"]
            return True

        async def run(request):
            self.assertEqual(request["argv"], ["example"])
            return result()

        self.assertEqual((await self.exercise(approve, run))["one"], result())

    async def test_mismatched_result_rejected(self):
        async def approve(*args):
            return True

        async def run(request):
            return result("other")

        with self.assertRaisesRegex(ValueError, "mismatched"):
            await self.exercise(approve, run)

    async def test_failure_is_signalled_to_scheduler(self):
        async def approve(*args):
            return True

        async def run(request):
            return result(status="failed")

        async def execute(engine, items, dispatch, concurrency):
            message = await dispatch(items[0]["name"], items[0]["arguments"])
            self.assertTrue(message.startswith("Error: "))
            return {"one": message}

        with patch("djcode.adapter_workflow.WorkflowEngine.execute", execute):
            outcome = await execute_adapter_workflow([node()], approve, run)
        self.assertEqual(outcome["one"]["status"], "failed")

    async def test_failed_exit_zero_rejected(self):
        async def approve(*args):
            return True

        async def run(request):
            value = result(status="failed")
            value["exit_code"] = 0
            return value

        with self.assertRaisesRegex(ValueError, "invalid"):
            await self.exercise(approve, run)

    async def test_request_id_must_match_node(self):
        item = node()
        item["arguments"]["id"] = "other"
        with self.assertRaisesRegex(ValueError, "matching"):
            await execute_adapter_workflow([item], None, None)


if __name__ == "__main__":
    unittest.main()
