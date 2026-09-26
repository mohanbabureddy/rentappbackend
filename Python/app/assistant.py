import json
import logging
import os
import re
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional

import anthropic
import requests

from app.models import TenantBill, User, utc_now
from app.repositories import DepositRepository, TenantBillRepository, UserRepository

logger = logging.getLogger("app.assistant")

MODEL = "claude-opus-5"

DEPOSIT_QUESTION = re.compile(r"deposit|advance", re.I)
REFUND_QUESTION = re.compile(r"refund|withdraw|return|\bback\b", re.I)
BILLS_QUESTION = re.compile(r"\bbills?\b|\bowe\b|\bdues?\b|outstanding|pending amount|unpaid|how much .*pay", re.I)
# Specifically about what's still owed, not a plain "my bills" -- in that case the
# reply should show only the unpaid bills, not also clutter it with already-paid
# ones nobody asked about.
UNPAID_ONLY_QUESTION = re.compile(r"\bunpaid\b|\bpending\b|\boutstanding\b|\bdue\b|\bowe\b", re.I)
# A question that LOOKS like a simple "what are my bills" (matches BILLS_QUESTION
# above) but is actually asking something specific -- a particular month, a
# comparison, a superlative, an all-time question, or about a different tenant --
# needs the model (and the get_bills tool) to answer properly, not the generic
# "here are your unpaid/paid bills" summary. Without this, BILLS_QUESTION's broad
# wording (any mention of "bill" or "owe") swallows almost every realistic bill
# question before the model -- and the tool -- ever sees it.
BILLS_TARGETED_QUESTION = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|\b20\d{2}\b|"
    r"compare|\bvs\.?\b|versus|highest|lowest|maximum|minimum|\bmost\b|\bleast\b|average|"
    r"\bbefore\b|\bafter\b|\bsince\b|\bbetween\b|\bever\b|\bnever\b|"
    r"this month|last month|next month|\broom\s*\d+\b",
    re.I,
)
# A deposit question that ALSO asks about rent/electricity/bills is a compound
# question -- e.g. "my average rent, highest electricity bill, and deposit owed,
# all three". DEPOSIT_QUESTION alone would grab it and answer only the deposit
# part, silently dropping the rest. The model can answer all of it in one go: the
# deposit figures are already in its system prompt, and it has the get_bills tool
# for the rest -- so route the whole thing there instead of the deposit-only reply.
MULTI_TOPIC_QUESTION = re.compile(r"\brent\b|\belectricity\b|\bbills?\b", re.I)
IST_OFFSET = timedelta(hours=5, minutes=30)
MAX_TOOL_ROUNDS = 3  # hard cap so a confused model can't loop forever (cost/latency safety)

# The one tool the model can call for anything not already covered by the fixed
# replies above (deposit/bills/refund questions never reach here at all). It can
# only filter bills already loaded for the CURRENT, authenticated tenant -- there
# is no "tenant" parameter, so the model has no way to ask for anyone else's data.
GET_BILLS_TOOL = {
    "name": "get_bills",
    "description": (
        "Look up this tenant's own bills, going further back than the recent summary "
        "already given to you. Use this for anything about a specific past month, a "
        "specific bill type, or a total/average/comparison/highest/lowest across many "
        "months. Call it with NO arguments to get every bill on record -- always do this "
        "for 'ever', 'always', 'never', or 'did I ever miss a payment' style questions, and "
        "for anything about how far back the records go, or a date that might be before any "
        "bill exists -- since the summary above only covers the last 12 months and does NOT "
        "tell you when the tenant's history actually starts. Never answer a question about "
        "history, totals, 'ever', or the earliest/oldest bill from the summary alone, and "
        "never guess or state when records 'start' without calling this first."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "month": {"type": "string", "description": "YYYY-MM, e.g. 2026-03. Omit for every month."},
            "bill_type": {"type": "string", "enum": ["RENT", "ELECTRICITY"], "description": "Omit for both types."},
        },
    },
}


def _ollama_tool_format(tool: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "function", "function": {
        "name": tool["name"], "description": tool["description"], "parameters": tool["input_schema"],
    }}


def _rupees(amount) -> str:
    amount = float(amount or 0)
    return f"Rs.{amount:,.0f}" if amount == int(amount) else f"Rs.{amount:,.2f}"


class AssistantService:
    """Answers a tenant's free-form questions (e.g. "who do I pay rent to") using
    only that tenant's own bill data -- never another tenant's, and never anything
    fabricated (bank/UPI details the app doesn't actually have on file)."""

    def __init__(self, bill_repo: TenantBillRepository, user_repo: UserRepository, deposit_repo: DepositRepository):
        self.bill_repo = bill_repo
        self.deposit_repo = deposit_repo
        self.user_repo = user_repo
        self._provider = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()
        # Old local defaults (kept for reference):
        # self._ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
        # self._ollama_model = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
        self._ollama_url = os.getenv("OLLAMA_URL", "https://ollama.com").rstrip("/")
        self._ollama_model = os.getenv("OLLAMA_MODEL", "gpt-oss:20b")
        self._ollama_api_key = os.getenv("OLLAMA_API_KEY")
        api_key = os.getenv("ANTHROPIC_API_KEY")
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else None

    @staticmethod
    def _run_get_bills_tool(args: Dict[str, Any], bills: List[TenantBill]) -> str:
        """Executes get_bills for real -- filters the CURRENT tenant's already-loaded
        bills by whatever month/type the model asked for. There is no tenant argument
        here at all, so this can never be pointed at another tenant's data."""
        month = (args or {}).get("month") or None
        bill_type = ((args or {}).get("bill_type") or "").upper() or None
        matched = [b for b in bills if (not month or b.month_year == month) and (not bill_type or b.bill_type == bill_type)]
        return json.dumps([
            {
                "month": b.month_year, "type": b.bill_type, "paid": b.paid,
                "rent": b.rent, "water": b.water, "electricity": b.electricity, "miscellaneous": b.miscellaneous,
                "total": (b.rent or 0) + (b.water or 0) + (b.electricity or 0) + (b.miscellaneous or 0),
            }
            for b in matched
        ])

    def _run_tool(self, name: str, args: Dict[str, Any], bills: List[TenantBill]) -> str:
        if name == "get_bills":
            return self._run_get_bills_tool(args, bills)
        return json.dumps({"error": f"Unknown tool '{name}'"})

    def _ask_ollama(self, system_prompt: str, message: str, bills: List[TenantBill], trace: List[str]) -> str:
        headers = {"Authorization": f"Bearer {self._ollama_api_key}"} if self._ollama_api_key else {}
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
        ]
        for _ in range(MAX_TOOL_ROUNDS):
            resp = requests.post(
                f"{self._ollama_url}/api/chat",
                headers=headers,
                json={
                    "model": self._ollama_model,
                    "stream": False,
                    "messages": messages,
                    "tools": [_ollama_tool_format(GET_BILLS_TOOL)],
                },
                timeout=120,
            )
            resp.raise_for_status()
            msg = resp.json().get("message", {})
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                return msg.get("content", "")
            messages.append(msg)
            for call in tool_calls:
                fn = call.get("function", {})
                trace.append(f"Model called tool: {fn.get('name')}({fn.get('arguments')})")
                result = self._run_tool(fn.get("name"), fn.get("arguments") or {}, bills)
                messages.append({"role": "tool", "content": result})
        return "Sorry, I couldn't work out an answer to that."

    def _ask_anthropic(self, system_prompt: str, message: str, bills: List[TenantBill], trace: List[str]) -> str:
        messages: List[Dict[str, Any]] = [{"role": "user", "content": message}]
        for _ in range(MAX_TOOL_ROUNDS):
            response = self._client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=system_prompt,
                output_config={"effort": "low"},
                tools=[GET_BILLS_TOOL],
                messages=messages,
            )
            if response.stop_reason != "tool_use":
                return next((block.text for block in response.content if block.type == "text"), "")
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    trace.append(f"Model called tool: {block.name}({block.input})")
                    result = self._run_tool(block.name, block.input, bills)
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
            messages.append({"role": "user", "content": tool_results})
        return "Sorry, I couldn't work out an answer to that."

    @staticmethod
    def _bill_line(b: TenantBill) -> str:
        total = (b.rent or 0) + (b.water or 0) + (b.electricity or 0) + (b.miscellaneous or 0)
        if b.bill_type == "ELECTRICITY":
            return f"{b.month_year} Electricity: {_rupees(total)}"
        parts = [f"rent {_rupees(b.rent)}", f"water {_rupees(b.water)}"]
        if b.miscellaneous:
            parts.append(f"misc {_rupees(b.miscellaneous)}")
        return f"{b.month_year} Rent: {_rupees(total)} ({', '.join(parts)})"

    def _bills_reply(self, bills: List[TenantBill], unpaid_only: bool = False) -> str:
        """Exact bill summary from the database (not model-generated). `unpaid_only`
        drops the "Recently paid" section -- someone who specifically asked for
        unpaid/pending/due bills shouldn't have to read past ones they already paid."""
        if not bills:
            return "You have no bills yet."
        ordered = sorted(bills, key=lambda b: (b.month_year or "", b.bill_type or ""), reverse=True)
        unpaid = [b for b in ordered if not b.paid]
        paid = [] if unpaid_only else [b for b in ordered if b.paid][:3]
        lines = []
        if unpaid:
            due = sum((b.rent or 0) + (b.water or 0) + (b.electricity or 0) + (b.miscellaneous or 0) for b in unpaid)
            lines.append("Unpaid bills:")
            lines += [f"- {self._bill_line(b)}" for b in unpaid]
            lines.append(f"Total due: {_rupees(due)}")
            lines.append("Pay from the My Bills page using the Pay button on each bill.")
        else:
            lines.append("You have no unpaid bills. All paid up!")
        if paid:
            lines += ["", "Recently paid:"]
            lines += [f"- {self._bill_line(b)}" for b in paid]
        return "\n".join(lines)

    def _deposit_reply(self, tenant: User) -> str:
        """Exact deposit summary + date-wise payments, built from the ledger
        (not model-generated, so amounts and dates are never paraphrased wrongly)."""
        history = sorted(
            self.deposit_repo.find_by_tenant_order_by_date_desc(tenant.username),
            key=lambda p: p.paid_date,
        )
        paid = self.deposit_repo.total_for_tenant(tenant.username)
        demanded = tenant.demanded_deposit
        lines = ["Your security deposit:"]
        if demanded is not None:
            lines += [
                f"Demanded: {_rupees(demanded)}",
                f"Paid so far: {_rupees(paid)}",
                f"Remaining: {_rupees(max(demanded - paid, 0))}",
            ]
        else:
            lines.append(f"Paid so far: {_rupees(paid)} (no demanded amount has been set)")
        if history:
            lines += ["", "Payments, date-wise:"]
            for p in history:
                when = (p.paid_date + IST_OFFSET).strftime("%d %b %Y, %I:%M %p")
                how = "Paid online" if p.source == "razorpay" else "Recorded by owner"
                note = f" ({p.notes})" if p.notes else ""
                lines.append(f"{when} - {_rupees(p.amount)} - {how}{note}")
        else:
            lines += ["", "No deposit payments recorded yet."]
        return "\n".join(lines)

    def _deposit_refund_reply(self) -> str:
        admin = self._admin_contact()
        lines = [
            "Security deposit refunds are not processed in this app. The owner settles your deposit with you directly when you move out.",
        ]
        if admin:
            contact = [admin.full_name or admin.username]
            if admin.phone:
                contact.append(f"phone {admin.phone}")
            if admin.mail:
                contact.append(f"email {admin.mail}")
            lines.append("Please contact the owner: " + ", ".join(contact) + ".")
        return "\n".join(lines)

    def _admin_contact(self) -> Optional[User]:
        for user in self.user_repo.find_all():
            if user.role == "ADMIN":
                return user
        return None

    def _build_system_prompt(self, tenant: User, bills: List[TenantBill]) -> str:
        admin = self._admin_contact()
        recent = sorted(bills, key=lambda b: b.month_year or "", reverse=True)[:12]
        bill_lines = []
        for b in recent:
            total = (b.rent or 0) + (b.water or 0) + (b.electricity or 0) + (b.miscellaneous or 0)
            status = "PAID" if b.paid else "UNPAID"
            if b.bill_type == "ELECTRICITY":
                bill_lines.append(
                    f"- {b.month_year} ELECTRICITY bill: amount=Rs.{b.electricity or 0}, status={status}"
                )
            else:
                bill_lines.append(
                    f"- {b.month_year} RENT bill: rent=Rs.{b.rent or 0}, water=Rs.{b.water or 0}, "
                    f"miscellaneous=Rs.{b.miscellaneous or 0}, total=Rs.{total}, status={status}"
                )
        bills_block = "\n".join(bill_lines) if bill_lines else "No bills on record."
        if len(bills) > len(recent):
            bills_block += (
                f"\n(These are only the {len(recent)} most recent bills. There are {len(bills)} in total -- "
                "call get_bills for anything older, or for an all-time/ever/average/total question.)"
            )

        admin_block = (
            f"Owner contact (always call this person 'the owner', never 'admin'): name={admin.full_name or admin.username}, "
            f"email={admin.mail or 'not on file'}, phone={admin.phone or 'not on file'}."
            if admin else "No owner contact is on file."
        )

        demanded = tenant.demanded_deposit
        paid_deposit = self.deposit_repo.total_for_tenant(tenant.username)
        if demanded is None:
            deposit_block = f"Security deposit: paid so far Rs.{paid_deposit}; no demanded amount is set."
        else:
            deposit_block = (
                f"Security deposit: demanded Rs.{demanded}, paid so far Rs.{paid_deposit}, "
                f"remaining Rs.{max(demanded - paid_deposit, 0)}. The tenant can pay deposit "
                "in instalments using 'Pay Deposit' at the top of the My Bills page."
            )
        deposit_block += (
            " Deposit refunds are NOT processed in this app -- the owner settles the deposit "
            "with the tenant directly when they move out; never say otherwise."
        )

        today = (utc_now() + IST_OFFSET).strftime("%Y-%m-%d")
        return (
            "You are the support assistant of a rent management app. You are NOT the tenant. "
            f"Today's date is {today} (Indian time) -- use this to work out what 'this month', "
            "'last month' etc. mean. "
            f"The person chatting with you is the tenant named '{tenant.full_name or tenant.username}'; address them "
            "as 'you'. Answer only about THIS tenant -- if asked about another tenant/room, say you can "
            "only help with their own account. Use ONLY the data below and the get_bills tool -- never invent "
            "bank account numbers, UPI IDs, or any payment detail that isn't given here. "
            "Rent and electricity are separate bills, each paid entirely inside this app via the 'Pay' button on the tenant's "
            "Bills page (a Razorpay checkout popup); there is no separate bank transfer or "
            "UPI payment to make. If asked something this data doesn't cover even after calling get_bills, say so "
            "honestly instead of guessing. If get_bills returns an empty list for what was asked, that means there "
            "is NO bill on record for it -- say so plainly; never substitute a nearby or typical month's amount. "
            "You cannot take any action (you cannot file complaints, send messages, or "
            "contact anyone); only answer questions from the data. Keep answers short and direct.\n\n"
            f"{admin_block}\n\n{deposit_block}\n\n"
            f"Tenant's bill history (most recent first):\n{bills_block}"
        )

    def ask(self, username: str, message: str) -> str:
        return self.ask_traced(username, message)[0]

    def ask_traced(self, username: str, message: str):
        """Returns (answer, trace). `trace` is a plain list of step descriptions
        showing exactly how the question was routed -- shown only in debug mode."""
        trace: List[str] = []
        if not message or not message.strip():
            raise ValueError("Message required")
        trace.append(f'Server received: "{message.strip()}"')

        tenant = self.user_repo.find_by_username(username)
        if tenant is None:
            raise ValueError("Tenant not found")
        trace.append(f"Identified tenant from login token: {tenant.username}")

        if DEPOSIT_QUESTION.search(message) and not MULTI_TOPIC_QUESTION.search(message):
            if REFUND_QUESTION.search(message):
                trace.append("Deposit word + refund/withdraw word found -> fixed refund reply, no AI model used")
                return self._deposit_refund_reply(), trace
            trace.append("Deposit word found (and nothing else) -> exact reply built from the deposit ledger in the database, no AI model used")
            return self._deposit_reply(tenant), trace
        trace.append("Not a deposit-only question -> not answered by the deposit fixed reply")

        bills = self.bill_repo.find_by_tenant_name_order_by_month_desc(username)
        trace.append(f"Loaded {len(bills)} bill(s) for this tenant from the database")
        if BILLS_QUESTION.search(message) and not BILLS_TARGETED_QUESTION.search(message):
            unpaid_only = bool(UNPAID_ONLY_QUESTION.search(message))
            trace.append(
                f"Generic bills word found (and nothing more specific) -> exact reply built from the bills "
                f"({'unpaid only' if unpaid_only else 'unpaid + recently paid'}), no AI model used"
            )
            return self._bills_reply(bills, unpaid_only=unpaid_only), trace
        trace.append("Not a generic bills question (or it's a targeted one) -> the AI model will answer, with the get_bills tool available")

        if self._provider != "ollama" and self._client is None:
            raise RuntimeError("The assistant isn't configured yet (missing ANTHROPIC_API_KEY in .env).")
        system_prompt = self._build_system_prompt(tenant, bills)
        trace.append(f"Built the system prompt ({len(system_prompt)} characters) from the tenant's data:\n{system_prompt}")

        logger.info("Assistant question from '%s': %s", username, message[:200])
        provider = f"Ollama model {self._ollama_model}" if self._provider == "ollama" else f"Anthropic model {MODEL}"
        trace.append(f"Sending system prompt + question to {provider}, with the get_bills tool available")
        started = time.monotonic()
        try:
            if self._provider == "ollama":
                answer = self._ask_ollama(system_prompt, message.strip(), bills, trace)
            else:
                answer = self._ask_anthropic(system_prompt, message.strip(), bills, trace)
        except (anthropic.APIError, requests.RequestException):
            logger.exception("Assistant API call failed for '%s'.", username)
            raise RuntimeError("Assistant is temporarily unavailable. Please try again shortly.")

        trace.append(f"Model replied with {len(answer)} characters in {time.monotonic() - started:.1f}s")
        logger.info("Assistant answered '%s' (%d chars).", username, len(answer))
        return answer, trace
