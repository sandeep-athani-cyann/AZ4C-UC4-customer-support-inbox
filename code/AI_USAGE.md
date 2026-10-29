# How I used AI coding tools

**Tool:** Claude Code (Claude Opus 5.5) in VS Code.

**What it did**

- Read the whole pack (use case, policies, tone guide, 30 help articles, 200 emails, tables, schema,
  samples) and turned the policies into the agent split, one agent per policy area.
- Joined every email to its order, shipment, item and refund rows to find the edge cases (late
  electronics, hygiene items, already-refunded items, pending vs dispatched shipments, sender/owner
  mismatches, withdrawn requests in threads, injected instructions).
- Wrote the code in `app/`, the docs and the test cases. The LLM client, trace and scorer follow the
  same design as my UC3 build.
- Ran the system over all 200 emails and went through each verdict and a sample of draft replies.

**What I checked or changed myself**

- The expected answers in `tests/expected/` were worked out from the policies and the raw rows, not
  copied from the system's output.
- Chose to keep decisions and amounts free of the LLM, so text in an email cannot steer them (EP 4).
  The LLM only labels emails the rules cannot read and rewrites the reply, and both outputs are checked.
- Fixed problems found in the full run: a "when my other order … will arrive" question that was not
  recognised, a legal-threat email whose requests were being answered instead of handed over, a
  signature check that greeted a customer as "Hi Thanks", a warranty offer on a physically damaged
  item (HA-14 excludes physical damage), and stiff reply wording.
- Tested the approval flow by hand: pause (nothing written), approve, a repeated approve (refused),
  reject (nothing written), change with an amount above the policy amount (refused) and a lower one.
- Tested the LLM fallback with invalid keys: Groq returned 401, Gemini returned 400, and the run
  finished with the template reply. Tested the reply check and the labelling check with stubbed model
  replies (an extra voucher amount and a dropped article were rejected; an invented sentence and an
  unknown label were dropped). A real model reply has not been tested because no API keys were available.
- Checked that every result matches the schema, that the data pack files still match `pack.json`
  checksums after all runs, and that no key or provider address is hard-coded.
