# Architecture

```mermaid
flowchart TD
    CASE["case.json<br/>{email_id}"] --> ORCH

    subgraph ORCH["Orchestrator (app/orchestrator.py)"]
        direction TB
        IN["IntakeAgent<br/>email, sender account, thread<br/>EP 1 escalation · EP 4 injected text"] --> EX["ExtractorAgent<br/>one request per ask, tied to order + item<br/>(LLM only if rules find nothing)"]
        EX --> ID["IdentityAgent<br/>RP 1.1 sender = order owner?"]
        ID --> RES["ResolverAgent<br/>RP 2 refunds · RP 3 address/cancel<br/>RP 4 help articles · order status"]
        RES --> DEC["DecisionAgent<br/>RP 5 decision, reason, priority<br/>RP 6 / EP 3 approval"]
        DEC --> REP["ReplyAgent<br/>template → LLM rewrite → checked"]
    end

    subgraph TOOLS["Read-only tools (app/tools.py)"]
        DB[("store.sqlite<br/>opened read-only")]
        DOCS["docs/<br/>emails · help articles · policies"]
    end
    IN & EX & ID & RES -. traced calls .-> TOOLS

    REP -->|"refund or handover,<br/>no --auto-approve"| PAUSE["runs/&lt;run_id&gt;.json<br/>waiting for support lead"]
    PAUSE -->|"approve / change"| WRITE
    PAUSE -->|reject| NOTHING["nothing written"]
    REP -->|"plain reply,<br/>or --auto-approve"| WRITE["mock_system/<br/>helpdesk_tickets.jsonl<br/>refund_requests.jsonl"]

    EX -.-> LLM["Groq → Gemini<br/>(retry, then template)"]
    REP -.-> LLM
    ORCH -.-> TRACE["traces/&lt;trace_id&gt;.jsonl"]
```

Every agent always runs, because one email can hold several requests. Two short-circuits apply:
an EP 1 trigger hands every request over without answering it, and an RP 1.1 mismatch stops that
order's details from reaching the reply. The LLM never sees raw email text when it writes the reply
(only the template built from checked facts), so instructions inside an email cannot reach it (EP 4).
