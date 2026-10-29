"""Append-only JSONL trace of every agent step and tool call in a run."""
import json
import time
from datetime import datetime, timezone

from . import config


class Trace:
    def __init__(self, trace_id: str):
        self.trace_id = trace_id
        config.TRACES_DIR.mkdir(parents=True, exist_ok=True)
        self.path = config.TRACES_DIR / f"{trace_id}.jsonl"

    def log(self, kind: str, name: str, **data) -> None:
        record = {
            "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "trace_id": self.trace_id,
            "kind": kind,
            "name": name,
            **data,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def timed(self, kind: str, name: str, fn, **args):
        """Run fn(**args), log its arguments, a short result summary and the duration."""
        start = time.perf_counter()
        try:
            result = fn(**args)
        except Exception as e:
            self.log(kind, name, args=args, error=f"{type(e).__name__}: {e}",
                     ms=round(1000 * (time.perf_counter() - start), 1))
            raise
        if isinstance(result, list):
            summary = f"{len(result)} rows"
        elif isinstance(result, dict) and "text" in result:
            summary = {k: v for k, v in result.items() if k != "text"}
        else:
            summary = result
        self.log(kind, name, args=args, result=summary, ms=round(1000 * (time.perf_counter() - start), 1))
        return result
