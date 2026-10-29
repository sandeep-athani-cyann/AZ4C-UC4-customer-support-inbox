"""Scores a result against an expected answer, field by field, as described in the pack README:
decision, reason_code (any accepted code), amount (within tolerance), key_numbers (where present),
must_cite and needs_human. Each scored field counts equally; must_cite and key_numbers give partial credit.
"""
import re

NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _numbers(obj, out: list) -> list:
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out.append(float(obj))
    elif isinstance(obj, str):
        out += [float(n) for n in NUM_RE.findall(obj.replace(",", ""))]
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _numbers(v, out)
    return out


def _amount_ok(got, exp, tolerance) -> bool:
    if isinstance(exp, dict):
        exp, tolerance = exp.get("value"), exp.get("tolerance", tolerance)
    if exp is None:
        return got is None
    return got is not None and abs(float(got) - float(exp)) <= float(tolerance)


def score_details(result: dict, expected: dict) -> dict:
    parts = {}
    if "decision" in expected:
        parts["decision"] = float(result.get("decision") == expected["decision"])
    if "reason_code" in expected:
        accepted = expected["reason_code"] if isinstance(expected["reason_code"], list) else [expected["reason_code"]]
        parts["reason_code"] = float(result.get("reason_code") in accepted)
    if "amount" in expected:
        parts["amount"] = float(_amount_ok(result.get("amount"), expected["amount"],
                                           expected.get("amount_tolerance", 0.01)))
    if expected.get("key_numbers"):
        nums = _numbers(result, [])
        hits = sum(any(abs(n - float(k)) <= max(0.01, abs(float(k)) * 0.005) for n in nums)
                   for k in expected["key_numbers"])
        parts["key_numbers"] = hits / len(expected["key_numbers"])
    if "must_cite" in expected:
        cites = set(result.get("citations") or [])
        need = expected["must_cite"]
        parts["must_cite"] = sum(c in cites for c in need) / len(need) if need else 1.0
    if "needs_human" in expected:
        parts["needs_human"] = float(result.get("needs_human") == expected["needs_human"])
    return parts


def score_case(result: dict, expected: dict) -> float:
    parts = score_details(result, expected)
    return round(sum(parts.values()) / len(parts), 4) if parts else 0.0
