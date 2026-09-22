import unittest

from app.assistant import BILLS_QUESTION, BILLS_TARGETED_QUESTION, IST_OFFSET
from app.models import utc_now


class BillsRoutingTest(unittest.TestCase):
    """Whether a message should get the instant fixed bills summary (BILLS_QUESTION
    matches and BILLS_TARGETED_QUESTION doesn't), or needs the model + get_bills tool.
    Found via a real bug: generic wording like "bill"/"owe" was swallowing specific
    questions ("highest bill", "bill in March", "compare this/last month") before
    the model -- and the new tool -- ever saw them."""

    def _wants_fixed_reply(self, message: str) -> bool:
        return bool(BILLS_QUESTION.search(message)) and not BILLS_TARGETED_QUESTION.search(message)

    def test_generic_phrasings_still_get_the_instant_fixed_reply(self):
        for msg in ("My bills", "my bills", "what do I owe", "any pending dues?", "show unpaid bills"):
            self.assertTrue(self._wants_fixed_reply(msg), msg)

    def test_targeted_phrasings_go_to_the_model_instead(self):
        targeted = [
            "What was my highest electricity bill and which month was it?",
            "What was my electricity bill in March 2026?",
            "How much total rent did I pay from January to June 2025?",
            "What is my average monthly electricity bill?",
            "Compare my electricity bill this month to last month.",
            "What does Room2 owe this month?",
            "What was my rent before the increase in July 2025?",
        ]
        for msg in targeted:
            self.assertFalse(self._wants_fixed_reply(msg), msg)

    def test_non_bill_questions_are_unaffected(self):
        for msg in ("who is the owner", "did I ever miss a rent payment", "hi"):
            self.assertFalse(BILLS_QUESTION.search(msg), msg)


class SystemPromptDateGroundingTest(unittest.TestCase):
    def test_todays_date_is_stated_in_the_prompt(self):
        from types import SimpleNamespace as N
        from app.assistant import AssistantService
        svc = AssistantService(None, N(find_all=lambda: []), N(total_for_tenant=lambda u: 0))
        tenant = N(username="Room1", full_name="Ravindra", demanded_deposit=None)
        prompt = svc._build_system_prompt(tenant, [])
        # Must be worked out from UTC+IST, not the test machine's own local clock
        # (which may already be IST, e.g. this dev PC -- that bug bit the real code too).
        expected = (utc_now() + IST_OFFSET).strftime("%Y-%m-%d")
        self.assertIn(f"Today's date is {expected}", prompt)

    def test_prompt_flags_when_older_bills_exist_beyond_the_recent_window(self):
        from types import SimpleNamespace as N
        from app.assistant import AssistantService
        svc = AssistantService(None, N(find_all=lambda: []), N(total_for_tenant=lambda u: 0))
        tenant = N(username="Room1", full_name="Ravindra", demanded_deposit=None)
        bills = [N(month_year=f"2025-{m:02d}", bill_type="RENT", rent=1000, water=0, electricity=None,
                    miscellaneous=0, paid=True) for m in range(1, 14)]  # 13 months > the 12-month window
        prompt = svc._build_system_prompt(tenant, bills)
        self.assertIn("call get_bills for anything older", prompt)


class DepositCompoundQuestionTest(unittest.TestCase):
    """A deposit question that ALSO asks about rent/electricity/bills is compound --
    it must not be swallowed by the deposit-only fixed reply, which would silently
    drop the rest of the question. Found via a real example: "my average rent, my
    highest electricity bill, and how much deposit do I still owe -- all three?"
    answered only the deposit part."""

    def _wants_deposit_only_reply(self, message: str) -> bool:
        from app.assistant import DEPOSIT_QUESTION, MULTI_TOPIC_QUESTION
        return bool(DEPOSIT_QUESTION.search(message)) and not MULTI_TOPIC_QUESTION.search(message)

    def test_pure_deposit_questions_still_get_the_instant_fixed_reply(self):
        for msg in ("how much deposit do I still owe", "show my deposit history", "deposit balance?"):
            self.assertTrue(self._wants_deposit_only_reply(msg), msg)

    def test_compound_questions_go_to_the_model_instead(self):
        compound = [
            "What's my average rent, my highest electricity bill, and how much deposit do I still owe -- all three?",
            "I want to withdraw my deposit and also check my rent for this month.",
        ]
        for msg in compound:
            self.assertFalse(self._wants_deposit_only_reply(msg), msg)

    def test_refund_policy_is_always_in_the_prompt_even_off_the_fixed_reply_path(self):
        from types import SimpleNamespace as N
        from app.assistant import AssistantService
        svc = AssistantService(None, N(find_all=lambda: []), N(total_for_tenant=lambda u: 0))
        tenant = N(username="Room1", full_name="Ravindra", demanded_deposit=25000.0)
        prompt = svc._build_system_prompt(tenant, [])
        self.assertIn("refunds are NOT processed in this app", prompt)


class GetBillsToolHistoryBoundsTest(unittest.TestCase):
    def test_tool_description_warns_against_guessing_when_history_starts(self):
        from app.assistant import GET_BILLS_TOOL
        self.assertIn("never guess or state when records 'start'", GET_BILLS_TOOL["description"])


if __name__ == "__main__":
    unittest.main()
