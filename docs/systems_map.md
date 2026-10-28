# Systems map

```mermaid
flowchart LR
  Ashby[Ashby<br/>offer / candidate] -- "webhook (breaks today: manual re-keying)" --> n8n
  Workday[Workday<br/>system of record] -- "webhook / report" --> n8n
  n8n[n8n / Zapier<br/>orchestrates triggers] --> Engine
  subgraph Engine["Sync engine (THE SLICE I BUILT)"]
    direction TB
    I[ingest + dedupe + stale check] --> V[match + validate V1-V8] --> D[diff + plan] --> X[idempotent executor]
    X --> A[(SQLite: ledger + audit)]
  end
  Claude[Claude<br/>exception triage only] <--> Engine
  Engine -- "Ashby-originated changes only" --> Workday
  Engine --> Okta
  Engine --> Lumos
  Engine --> Equip[Equipment form / shipping]
  Engine --> Tracker[Cohort Tracker - Google Sheet]
  Engine -- "exceptions + approvals" --> Slack
  Slack -- "approve / reject" --> Engine

  classDef gap stroke:#d33,stroke-width:2px,stroke-dasharray:4 3;
  class Ashby,Workday,Equip,Tracker gap;
```

Red dashed = where data breaks today (changes made after the offer don't reach downstream systems reliably).

| System | Data owned | How it changes | Failure mode today |
|---|---|---|---|
| Ashby | Candidate, offer, start date, shipping address (pre-hire) | Recruiter edits after offer | Edits never reach Okta/IT/Tracker; stale copies |
| Workday | Worker record, manager, location (system of record) | HR edits; off-cycle hires created directly | Off-cycle hires invisible to other systems; Ashby/Workday drift |
| Okta | Login, display name, groups, activation date | Provisioned from hire data | Wrong login/name if source changed; active-account renames are risky |
| Lumos | App bundle, access schedule, approver | Requested per role/location/manager | Wrong bundle/approver after location or manager change |
| Equipment (IT form) | Hardware request, ship-to, ship-by, shipped flag | Manual form | Laptop shipped to old address / too late |
| Cohort Tracker | Cohort week, roster row | Manual sheet edits | Silent drift, duplicate rows |
| Slack | Exceptions, approvals | Human clicks | Exceptions lost in channel noise |
| n8n | Triggers / glue | Workflow edits | No state: retries and ordering are its weak spot |
