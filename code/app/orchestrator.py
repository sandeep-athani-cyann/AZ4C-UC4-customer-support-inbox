"""Orchestrator: runs the agents in policy order, drafts one reply, pauses for the support lead
when a refund or handover needs approval, and writes to the mock helpdesk.

    intake (EP 1, EP 4) -> extractor -> identity (RP 1) -> resolver (RP 2-4) -> decision (RP 5-6) -> reply
"""
import json
import os
import re
import uuid
from datetime import datetime, timezone

from . import config, llm
from .agents import (Context, DecisionAgent, ExtractorAgent, IdentityAgent, IntakeAgent, ResolverAgent,
                     damage_deadline, money)
from .tools import ORDER_RE, ToolBox, ToolError
from .trace import Trace

PIPELINE = [IntakeAgent(), ExtractorAgent(), IdentityAgent(), ResolverAgent()]
SIGN_OFF = "Team Lumora Home"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReplyAgent:
    """Drafts one reply that answers every request in the order asked (tone_guide.md).
    The template is built from the resolved requests. When a key is set, the LLM rewrites it for tone;
    the rewrite is accepted only if it keeps every amount, order ID and article ID and adds none."""

    name = "reply"

    SYSTEM = ("You polish customer-support email replies for Lumora Home, an online home-goods store. Rewrite the "
              "draft so it reads warm and plain: use the customer's first name, short paragraphs, no jargon, no blame. "
              "Keep every fact, amount, date, order ID and help-article ID exactly as given and in the same order. "
              "Do not add facts, offers, promises, amounts or apologies for fault. If 'hinglish' is true you may add "
              "a light friendly Hinglish touch. Sign off exactly as \"Team Lumora Home\". Reply with the email text only.")

    def run(self, ctx: Context, v: dict) -> tuple[str, str]:
        template = self.template(ctx, v)
        if v["decision"] == "handover" or not config.use_llm():
            return template, "template"   # EP 2: the handover text is fixed
        try:
            text, model = llm.chat([
                {"role": "system", "content": self.SYSTEM},
                {"role": "user", "content": json.dumps({"customer_first_name": ctx.name, "hinglish": ctx.hinglish,
                                                        "draft": template}, ensure_ascii=False)},
            ], trace=ctx.tools.trace)
            problem = self.check(text, template)
            if problem:
                ctx.tools.trace.log("fallback", "reply", error=f"LLM reply rejected: {problem}")
                return template, "template"
            return text.strip(), model
        except llm.LLMError as e:
            ctx.tools.trace.log("fallback", "reply", error=str(e))
            return template, "template"

    @staticmethod
    def check(text: str, template: str) -> str | None:
        def ids(s):
            return set(re.findall(r"\bHA-\d+\b", s)) | set(ORDER_RE.findall(s))

        def amounts(s):
            return {float(a.replace(",", "")) for a in re.findall(r"₹\s?(\d[\d,]*(?:\.\d+)?)", s)}
        if ids(text) != ids(template):
            return f"IDs differ: {sorted(ids(text) ^ ids(template))}"
        if amounts(text) != amounts(template):
            return "amounts differ"
        if SIGN_OFF not in text:
            return "sign-off missing"
        return None

    def template(self, ctx: Context, v: dict) -> str:
        hello = f"Hi {ctx.name},"
        if v["decision"] == "handover":
            subject = ctx.email["subject"] if ctx.email else "your request"
            return (f"{hello}\n\nThank you for your email about \"{subject}\". We have received it, and a senior member "
                    f"of our team will respond to you within one working day.\n\n{SIGN_OFF}")
        paras, mismatch_said = [], False
        for r in ctx.requests:
            if r["outcome"] == "identity_mismatch":
                if not mismatch_said:
                    paras.append("For your security, we can only discuss an order with the customer who placed it, "
                                 "and only from the email address registered on that account. Please write to us "
                                 "from that address and we'll be glad to help straight away.")
                    mismatch_said = True
                continue
            paras.append(self.paragraph(ctx, r))
        if not paras:
            paras.append("Thank you for writing to us. Could you tell us a little more about how we can help?")
        close = "Dhanyavaad!\n\n" if ctx.hinglish else ""
        return f"{hello}\n\n" + "\n\n".join(p for p in paras if p) + f"\n\n{close}{SIGN_OFF}"

    @staticmethod
    def paragraph(ctx: Context, r: dict) -> str:
        o, oid = r["outcome"], r.get("order_id")
        order = ctx.orders.get(oid) if oid else None
        s = (order or {}).get("shipment") or {}
        what = r.get("product") or (order["items"][0]["name"] if order and len(order["items"]) == 1 else "your item")
        if o == "withdrawn":
            return f"Noted, you're keeping your order{' ' + oid if oid else ''}, so we've closed the earlier return request."
        if o == "need_order_id":
            return "Could you share your order number (it starts with LH-)? We'll check it for you right away."
        if o == "order_not_found":
            return f"We couldn't find an order {oid}. Could you check the number on your order confirmation?"
        if r["type"] in ("refund_request", "damaged_item"):
            if o == "eligible":
                names = ", ".join(i["name"] for i in r["items"])
                if r["rule"] == "RP 2.3":
                    start = f"We're sorry the {names} from order {oid} arrived damaged, and thank you for the photo."
                else:
                    start = f"Your return of the {names} from order {oid} is within our return window."
                return (f"{start} A refund of {money(r['amount'])} is due. We'll arrange a pickup within 3 working days "
                        f"(HA-04), and the money goes back to your original payment method: 5–7 working days for UPI "
                        f"and cards, 7–10 for net banking (HA-05).")
            if o == "photo_needed":
                return (f"We're sorry the {what} from order {oid} arrived damaged. Please reply with a photo of the "
                        f"item and its packaging by {damage_deadline(s)} and we'll arrange a refund or "
                        "replacement (HA-06).")
            return f"Thank you for writing to us about order {oid}. " + r["why"]
        if r["type"] == "refund_status":
            done = [f for f in order["refunds"]] if order else []
            if o == "refund_found":
                parts = "; ".join(f"{money(f['amount'])} was {f['status']} on {f['created_on']} (reference "
                                  f"{f['refund_id']})" for f in done)
                return (f"Good news: the refund for order {oid} has been processed: {parts}. Refunds reach your "
                        "original payment method in 5–7 working days for UPI and cards, or 7–10 for net banking "
                        "(HA-05). If you can't see it, your bank can trace it with the reference.")
            return (f"We can't see a refund for order {oid} in our system yet, which usually means your returned item "
                    "hasn't reached our warehouse. We've asked our returns team to check on your pickup (HA-04). Once "
                    "the refund is issued it reaches your original payment method in 5–7 working days for UPI and "
                    "cards, or 7–10 for net banking (HA-05).")
        if r["type"] == "order_status":
            if o == "status_delivered":
                return (f"Our courier {s['courier']} shows order {oid} as delivered on {s['delivered_on']} (tracking "
                        f"{s['tracking_no']}). If it hasn't reached you, please check with your neighbours or building "
                        "security and reply to this email; we'll raise it with the courier straight away (HA-01).")
            if o == "status_returned":
                return (f"We're sorry, the courier couldn't deliver order {oid} and it has come back to us. Our team "
                        "will contact you to arrange delivery again or a refund.")
            if o == "status_dispatched":
                return (f"Your order {oid} left our warehouse on {s['dispatched_on']} with {s['courier']}. Your "
                        f"tracking number is {s['tracking_no']}: open My Orders, choose the order and tap Track "
                        "(HA-01). Delivery usually takes 2–4 days in metro cities, 4–7 days elsewhere and up to 10 "
                        "days for remote pin codes (HA-23).")
            if o == "status_pending":
                return (f"Your order {oid} is being packed and hasn't been dispatched yet. You'll get a tracking "
                        "number as soon as it leaves our warehouse (HA-01); delivery then usually takes 2–4 days in "
                        "metro cities and 4–7 days elsewhere (HA-23).")
            return f"We're checking the shipment for order {oid} with our warehouse and will update you shortly."
        if r["type"] == "address_change":
            if o == "address_changed":
                return f"Done: order {oid} hasn't left our warehouse yet, so we've updated the delivery address to {r['new_address']} (HA-02)."
            if o == "need_address":
                return f"Order {oid} hasn't been dispatched yet, so we can change the address. Please send us the full new address with the pin code (HA-02)."
            return (f"Order {oid} has already been dispatched, so we can't change the address any more. You can ask "
                    "the courier to hold it at their nearest hub instead (HA-02).")
        if r["type"] == "cancel_order":
            if o == "cancelled":
                cod = order["payment_method"].lower().startswith("cash")
                return (f"Done: order {oid} hadn't been dispatched yet, so we've cancelled it free of charge."
                        + ("" if cod else f" Your {order['payment_method']} payment will go back to the original "
                                          "payment method within 5–7 working days (HA-03)."))
            return (f"Order {oid} has already been dispatched, so it can't be cancelled any more (HA-03). Once it "
                    "arrives, you can return it from My Orders and we'll arrange a pickup (HA-04).")
        if r["type"] == "delivery_complaint":
            return ("We're really sorry about your delivery experience. That's not how parcels should be handled, and "
                    "we've shared your feedback with our courier partner.")
        if r["type"] == "feedback":
            return "Thank you so much for the kind words! We're glad you're enjoying it."
        if o == "no_source":
            return ("That's a good question, and we want to give you the right answer rather than a guess. A product "
                    "specialist will reply to you within 2 working days.")
        if r["type"] == "warranty":
            return r["resolution"]
        arts = [a for a in r.get("citations", []) if a.startswith("HA-")]
        return " ".join(f"Our help article {ha} ({ctx.article(ha)['title']}) says: {ctx.article(ha)['text']}"
                        for ha in arts)


def diagnose(case: dict, trace: Trace) -> tuple[Context, dict]:
    ctx = Context(case_id=case["case_id"], email_id=(case.get("input") or {}).get("email_id", ""), tools=ToolBox(trace))
    try:
        for agent in PIPELINE:
            trace.log("agent", agent.name, status="start")
            agent.run(ctx)
            trace.log("agent", agent.name, status="done")
        v = DecisionAgent().run(ctx)
    except (ToolError, KeyError, TypeError, ValueError) as e:
        # Missing or broken data: never guess. A person takes over.
        trace.log("error", "diagnose", error=f"{type(e).__name__}: {e}")
        ctx.findings.append(f"Could not complete the run: {type(e).__name__}: {e}")
        ctx.cite(ctx.email_id)
        ctx.tag("system_error")
        for r in ctx.requests:
            r.setdefault("outcome", "not_resolved")
            r.setdefault("resolution", "Not resolved: the run stopped on an error.")
        v = {"decision": "handover", "reason_code": "NO_SOURCE_AVAILABLE", "amount": None, "priority": "P2",
             "needs_human": True, "refund": None}
    trace.log("agent", "decision", **{k: v[k] for k in ("decision", "reason_code", "amount", "priority")})
    return ctx, v


def run_case(case: dict, auto_approve: bool, out_path: str | None = None) -> dict:
    run_id = f"{case['case_id']}-{uuid.uuid4().hex[:8]}"
    trace = Trace(run_id)
    trace.log("run", "start", case=case, auto_approve=auto_approve)
    ctx, v = diagnose(case, trace)
    reply, model = ReplyAgent().run(ctx, v)
    e = ctx.email or {}

    result = {
        "case_id": case["case_id"],
        "decision": v["decision"],
        "reason_code": v["reason_code"],
        "amount": v["amount"],
        "citations": ctx.citations,
        "needs_human": v["needs_human"],
        "trace_id": run_id,
        "run_id": run_id,
        "email_id": ctx.email_id,
        "from": e.get("from"),
        "subject": e.get("subject"),
        "customer_id": ctx.customer and ctx.customer["customer_id"],
        "priority": v["priority"],
        "tags": ctx.tags,
        "requests": [{k: r.get(k) for k in ("id", "type", "text", "order_id", "product", "outcome", "resolution",
                                            "amount", "citations")} for r in ctx.requests],
        "refund": v["refund"],
        "draft_reply": reply,
        "reply_model": model,
        "extractor": ctx.extractor,
        "policy_checks": ctx.findings,
        "ignored_instructions": ctx.injections,
    }

    if v["needs_human"] and not auto_approve:
        result["status"] = "awaiting_approval"
        config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
        state = {"run_id": run_id, "status": "awaiting_approval", "result": result,
                 "out_path": str(os.path.abspath(out_path)) if out_path else None}
        (config.RUNS_DIR / f"{run_id}.json").write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        trace.log("approval", "paused", decision=v["decision"], amount=v["amount"])
        what = f"a refund of {money(v['amount'])}" if v["decision"] == "reply_with_refund" else "a handover"
        print(f"Run {run_id} is waiting for the support lead to approve {what}.\n"
              f"  python -m app approve {run_id} --action approve|reject|change")
    else:
        approval = "auto_approved" if v["needs_human"] else "not_required"
        result["status"] = "approved" if v["needs_human"] else "done"
        write_mock(result, approval, approver="auto-approve" if v["needs_human"] else None)
        trace.log("approval", approval)
    trace.log("run", "end", decision=result["decision"], status=result["status"])
    return result


def approve(run_id: str, action: str, new_decision: str | None = None, amount: float | None = None,
            note: str | None = None) -> dict:
    path = config.RUNS_DIR / f"{run_id}.json"
    if not path.is_file():
        raise SystemExit(f"No paused run {run_id} in {config.RUNS_DIR}")
    state = json.loads(path.read_text(encoding="utf-8"))
    if state["status"] != "awaiting_approval":
        raise SystemExit(f"Run {run_id} is already {state['status']}")
    trace = Trace(run_id)
    result = state["result"]
    approver = os.environ.get("APPROVER") or "support lead"

    if action == "reject":
        # A rejected approval writes nothing to the mock system.
        result["status"] = state["status"] = "rejected"
        trace.log("approval", "rejected", approver=approver, note=note)
    else:
        if action == "change":
            if new_decision is None and amount is None:
                raise SystemExit("--action change needs --decision reply|handover and/or --amount")
            result["original"] = {k: result[k] for k in ("decision", "reason_code", "amount")}
            if new_decision == "handover":
                result.update(decision="handover", amount=None, refund=None, needs_human=True)
                result["draft_reply"] = (f"{result['draft_reply'].splitlines()[0]}\n\nThank you for your email. A senior "
                                         "member of our team will respond to you within one working day.\n\n"
                                         f"{SIGN_OFF}")
            elif new_decision == "reply":
                result.update(decision="reply", amount=None, refund=None)
                result["reply_needs_edit"] = True   # the draft was written for a refund
            if amount is not None:
                if result["decision"] != "reply_with_refund" or not result.get("refund"):
                    raise SystemExit("--amount applies only to a refund decision")
                cap = result["refund"]["amount"]
                if not 0 < amount <= cap:
                    raise SystemExit(f"--amount must be above 0 and at most the policy amount {money(cap)}")
                result["amount"] = result["refund"]["approved_amount"] = round(amount, 2)
                result["draft_reply"] = result["draft_reply"].replace(money(cap), money(result["amount"]))
            result["policy_checks"].append(f"Support lead changed {result['original']} to decision "
                                           f"{result['decision']}, amount {result['amount']}" + (f": {note}" if note else ""))
        result["status"] = state["status"] = "approved" if action == "approve" else "changed"
        write_mock(result, state["status"], approver=approver, note=note)
        trace.log("approval", state["status"], approver=approver, decision=result["decision"],
                  amount=result["amount"], note=note)

    state["result"] = result
    state["resolved_at"] = _now()
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    if state.get("out_path"):
        with open(state["out_path"], "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"Run {run_id}: {state['status']} ({result['decision']}"
          + (f", {money(result['amount'])}" if result.get("amount") else "") + ")")
    return result


def _append_once(path, record: dict, key: str) -> None:
    """Append a JSONL record unless one with the same key value is already there."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and json.loads(line).get(key) == record[key]:
                return
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_mock(result: dict, approval: str, approver: str | None, note: str | None = None) -> None:
    """Write the ticket and any approved refund, only once the decision is final (and approved)."""
    mock = config.mock_dir()
    suffix = result["run_id"].rsplit("-", 1)[-1].upper()
    ticket_id = "TCK-" + suffix
    created = _now()
    state = {"handover": "Handed over", "reply_with_refund": "Refund approved"}.get(result["decision"], "Replied")
    ticket = {
        "ticket_id": ticket_id,
        "run_id": result["run_id"],
        "case_id": result["case_id"],
        "created_at": created,
        "email_id": result["email_id"],
        "from": result["from"],
        "customer_id": result["customer_id"],
        "subject": result["subject"],
        "decision": result["decision"],
        "reason_code": result["reason_code"],
        "priority": result["priority"],
        "tags": result["tags"],
        "requests": [{k: r[k] for k in ("id", "type", "order_id", "outcome")} for r in result["requests"]],
        "amount": result["amount"],
        "draft_reply": result["draft_reply"],
        "citations": result["citations"],
        "assigned_to": "Support lead" if result["decision"] == "handover" else "Support agent",
        "approval": approval,
        "approved_by": approver,
        "note": note,
        "state": state,
        "trace_id": result["trace_id"],
    }
    _append_once(mock / "helpdesk_tickets.jsonl", ticket, "run_id")
    refund = result.get("refund")
    if result["decision"] == "reply_with_refund" and refund:
        record = {
            "refund_request_id": "RR-" + suffix,
            "run_id": result["run_id"],
            "ticket_id": ticket_id,
            "created_at": created,
            "customer_id": result["customer_id"],
            "amount": result["amount"],
            "currency": "INR",
            "payment_method": refund.get("payment_method"),
            "lines": refund["lines"],
            "reason_code": result["reason_code"],
            "approval": approval,
            "approved_by": approver,
            "note": note,
            "status": "approved",
        }
        _append_once(mock / "refund_requests.jsonl", record, "run_id")
