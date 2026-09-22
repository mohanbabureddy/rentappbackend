import json
import unittest
from types import SimpleNamespace as N
from unittest import mock

from app.assistant import GET_BILLS_TOOL, MAX_TOOL_ROUNDS, AssistantService


def _bill(month, kind, paid, rent=0, water=0, elec=0, misc=0):
    return N(month_year=month, bill_type=kind, paid=paid, rent=rent, water=water, electricity=elec, miscellaneous=misc)


def _service(bills, provider="anthropic"):
    users = N(
        find_by_username=lambda u: N(username=u, full_name="Ravindra", demanded_deposit=None),
        find_all=lambda: [N(role="ADMIN", username="mohan", full_name="Mohanbabu G", phone="9", mail="a@b.c")],
    )
    bill_repo = N(find_by_tenant_name_order_by_month_desc=lambda u: bills)
    deposit_repo = N(total_for_tenant=lambda u: 0)
    svc = AssistantService(bill_repo, users, deposit_repo)
    svc._provider = provider
    return svc


def _tool_use_block(name, args, block_id="tool1"):
    return N(type="tool_use", id=block_id, name=name, input=args)


def _text_block(text):
    return N(type="text", text=text)


class AnthropicToolLoopTest(unittest.TestCase):
    def test_model_calls_get_bills_then_answers(self):
        bills = [
            _bill("2026-01", "RENT", True, rent=5000),
            _bill("2026-03", "RENT", False, rent=6000, water=300),
        ]
        svc = _service(bills)
        create = mock.Mock(side_effect=[
            N(stop_reason="tool_use", content=[_tool_use_block("get_bills", {"month": "2026-03"})]),
            N(stop_reason="end_turn", content=[_text_block("Your March rent bill was Rs.6,300, unpaid.")]),
        ])
        svc._client = N(messages=N(create=create))

        answer = svc.ask("Room1", "what was my rent in march")

        self.assertIn("Rs.6,300", answer)
        self.assertEqual(create.call_count, 2)
        second_call_messages = create.call_args_list[1].kwargs["messages"]
        tool_result_msg = second_call_messages[-1]
        result_data = json.loads(tool_result_msg["content"][0]["content"])
        self.assertEqual(len(result_data), 1)
        self.assertEqual(result_data[0]["month"], "2026-03")

    def test_model_answers_without_calling_any_tool(self):
        svc = _service([_bill("2026-01", "RENT", True, rent=5000)])
        create = mock.Mock(return_value=N(stop_reason="end_turn", content=[_text_block("Hello!")]))
        svc._client = N(messages=N(create=create))

        answer = svc.ask("Room1", "hi")

        self.assertEqual(answer, "Hello!")
        self.assertEqual(create.call_count, 1)

    def test_runaway_tool_calls_are_capped(self):
        svc = _service([_bill("2026-01", "RENT", True, rent=5000)])
        create = mock.Mock(return_value=N(stop_reason="tool_use", content=[_tool_use_block("get_bills", {})]))
        svc._client = N(messages=N(create=create))

        answer = svc.ask("Room1", "keep asking")

        self.assertIn("couldn't work out", answer)
        self.assertEqual(create.call_count, MAX_TOOL_ROUNDS)


class OllamaToolLoopTest(unittest.TestCase):
    @staticmethod
    def _resp(payload):
        return N(raise_for_status=lambda: None, json=lambda: payload)

    def test_model_calls_get_bills_then_answers(self):
        bills = [_bill("2026-02", "ELECTRICITY", False, elec=850)]
        svc = _service(bills, provider="ollama")
        responses = [
            self._resp({"message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "get_bills", "arguments": {"bill_type": "ELECTRICITY"}}}
            ]}}),
            self._resp({"message": {"role": "assistant", "content": "Your electricity bill is Rs.850, unpaid."}}),
        ]

        with mock.patch("app.assistant.requests.post", side_effect=responses) as post:
            # Deliberately avoids "bill"/"owe"/"unpaid" etc. -- those already have an
            # instant fixed reply (tested elsewhere) and would never reach the model.
            answer = svc.ask("Room1", "what did I get charged for electricity in february")

        self.assertIn("Rs.850", answer)
        self.assertEqual(post.call_count, 2)
        second_payload = post.call_args_list[1].kwargs["json"]
        self.assertEqual(second_payload["messages"][-1]["role"], "tool")
        result_data = json.loads(second_payload["messages"][-1]["content"])
        self.assertEqual(result_data[0]["type"], "ELECTRICITY")

    def test_model_answers_without_calling_any_tool(self):
        svc = _service([_bill("2026-01", "RENT", True, rent=5000)], provider="ollama")

        with mock.patch("app.assistant.requests.post", return_value=self._resp({"message": {"content": "Hi there!"}})) as post:
            answer = svc.ask("Room1", "hi")

        self.assertEqual(answer, "Hi there!")
        self.assertEqual(post.call_count, 1)

    def test_runaway_tool_calls_are_capped(self):
        svc = _service([_bill("2026-01", "RENT", True, rent=5000)], provider="ollama")
        looping = self._resp({"message": {"tool_calls": [{"function": {"name": "get_bills", "arguments": {}}}]}})

        with mock.patch("app.assistant.requests.post", return_value=looping) as post:
            answer = svc.ask("Room1", "keep asking")

        self.assertIn("couldn't work out", answer)
        self.assertEqual(post.call_count, MAX_TOOL_ROUNDS)


class GetBillsToolSecurityTest(unittest.TestCase):
    def test_schema_has_no_way_to_target_another_tenant(self):
        # The tool the model sees has no "tenant"/"username" field, so there is no
        # argument it could ever pass to ask for someone else's data.
        props = GET_BILLS_TOOL["input_schema"]["properties"]
        self.assertNotIn("tenant", props)
        self.assertNotIn("username", props)
        self.assertEqual(set(props), {"month", "bill_type"})

    def test_filters_only_within_the_bills_it_was_given(self):
        svc = _service([])
        bills = [
            _bill("2026-01", "RENT", True, rent=5000),
            _bill("2026-01", "ELECTRICITY", False, elec=200),
            _bill("2026-02", "RENT", False, rent=5000),
        ]

        only_jan_rent = json.loads(svc._run_tool("get_bills", {"month": "2026-01", "bill_type": "RENT"}, bills))
        self.assertEqual([b["month"] for b in only_jan_rent], ["2026-01"])

        everything = json.loads(svc._run_tool("get_bills", {}, bills))
        self.assertEqual(len(everything), 3)

    def test_unknown_tool_name_is_handled_gracefully(self):
        svc = _service([])
        result = json.loads(svc._run_tool("delete_everything", {}, []))
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
