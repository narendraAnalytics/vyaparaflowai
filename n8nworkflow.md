# n8n Workflow Build — Detailed Report & Reusable Playbook

This documents how the **AI Cybersecurity Lead Intelligence & Sales Automation**
n8n project was actually built, session by session: the environment setup, the
method used to edit workflows, every error hit and how it was fixed, and the
general lessons worth carrying into the *next* n8n project. Written so you can
reuse the process, not just the specific fixes.

---

## 1. What was built (for context)

A 4-workflow n8n pipeline behind a Next.js frontend:

- **WF-1 Lead Intake** — form submission → validate/dedupe → enrich → AI
  analysis (Groq) → deterministic scoring → specialist routing → draft outreach
  email → notify specialist → await human approval.
- **WF-2 Approval** — webhook fired when a human clicks Approve/Reject in the
  UI → sends the outreach email (Resend) → seeds follow-ups → creates a CRM
  opportunity.
- **WF-3 Follow-up Cron** — daily schedule → escalates stale pending approvals
  (>48h) → sends Day-3/Day-7 nudge emails → moves stale leads to nurture.
- **WF-4 Reply Watcher** — Resend inbound-email webhook → matches the reply to
  a lead → cancels pending follow-ups → notifies the owner.

Stack: Next.js (frontend/dashboard) + n8n (orchestration) + Groq (AI) +
PostgreSQL/Neon (data) + Resend (email).

---

## 2. Starting a new session — the checklist

Every session that touches this project needs three things running:

1. **n8n** at `http://localhost:5678`. Nothing works until this is up —
   webhooks 404, the frontend's `/api/leads` route fails, everything.
2. **Frontend dev server**: `cd frontend && npm run dev` →
   `http://localhost:3000`.
   - **Gotcha**: if a route folder (e.g. `/login`, `/apply`) was added in a
     *previous* session and now 404s even though the file clearly exists on
     disk, the dev server process predates that folder. Turbopack doesn't
     always hot-discover new **top-level** route folders live — kill the dev
     server process and restart it. Same applies after any `.env.local`
     edit (Next only reads env vars at server start).
   - Find and kill the stale process on Windows:

     ```bash
     netstat -ano | grep ':3000' | grep LISTENING
     powershell -Command "Stop-Process -Id <PID> -Force"
     ```

3. **ngrok** — only needed when a workflow must receive a *real* inbound
   webhook from an external service (here: WF-4 receiving real Resend inbound
   emails). Installed at `C:\Users\ES\ngrok\ngrok.exe` on this machine —
   already installed, never reinstall.
   - Launching it is a foreground/interactive step for a reason: Claude
     Code's own auto-mode classifier treats "expose a local port publicly" as
     sensitive and blocks it as a background process. Ask the human to run it
     themselves: `! C:\Users\ES\ngrok\ngrok.exe http 5678` (the `!` prefix
     runs it directly in the chat session so its output — including the
     public URL — lands in the conversation).
   - **The public URL is ephemeral — it changes every ngrok restart.** Any
     external webhook registration pointing at the old URL goes silently
     dead: the third party (Resend, in this case) just stops delivering, with
     **no error surfacing anywhere**. After every ngrok restart, check the
     current tunnel and re-register the webhook (see §6).
   - Check the current tunnel URL without asking the human to repeat it:

     ```bash
     curl -s http://127.0.0.1:4040/api/tunnels
     ```

---

## 3. Two ways to edit an n8n workflow — and when each was actually available

This project used **both** methods across different sessions, because API
access wasn't consistently available. Try method A first; fall back to B.

### Method A — n8n-mcp REST API tools (preferred, when available)

Confirmed working directly against this instance on 2026-08-21:
`n8n_get_workflow`, `n8n_update_partial_workflow`, `n8n_executions`,
`n8n_list_workflows`, `n8n_validate_workflow`. No API key setup was needed —
the MCP server was just usable out of the box this session.

- **Changes save immediately and take effect immediately — no n8n restart
  needed.** This is the big advantage over Method B.
- `n8n_update_partial_workflow`'s `updateNode` operation needs the node
  identified by `nodeName` (or `nodeId`) and the changed fields under an
  `updates` key — **not** `changes`:

  ```json
  {
    "type": "updateNode",
    "nodeName": "Send Approved Email",
    "updates": { "parameters.text": "={{ ... }}" }
  }
  ```

  Dot-path field keys like `"parameters.text"` or `"parameters.jsCode"` work
  directly — no need to pass the whole `parameters` object back.
- Always run `n8n_validate_workflow` (`profile: "runtime"`) after an edit —
  it validates node configs, connections, and expressions and returns
  errors/warnings without needing to actually execute the workflow.
- Get just the node(s) you're editing with `mode: "filtered"` +
  `nodeNames: [...]` instead of pulling the full workflow — avoids
  client-side truncation on workflows with long Code-node source.

### Method B — CLI + direct sqlite edit (fallback, no API/MCP access)

Used in an earlier session when neither n8n-mcp tools nor an n8n API key were
available (n8n's REST API needs login/API-key auth — 401 without it, and
generating a key needs the UI).

1. Read the workflow row straight from `~/.n8n/database.sqlite`
   (`workflow_entity` table; `nodes`/`connections`/`settings` columns are
   JSON strings) using the `sqlite3` npm package already bundled at
   `.../node_modules/n8n/node_modules/sqlite3` (no `better-sqlite3` present
   on this instance).
2. Edit the parsed `nodes`/`connections` in a Node script; write the full
   workflow object (`id`, `name`, `active: true`, `nodes`, `connections`,
   `settings`) to a JSON file.
3. Import it: `n8n import:workflow --input=file.json`
   - **Do NOT** add `--activeState=fromJson` — n8n's own docs say this flag
     is "only supported in multi-main & queue mode"; on a single-instance
     setup it throws `"can only be used when n8n is running in queue or
     multi-main mode"`. By default, `import:workflow` **deactivates every
     imported workflow**.
4. Reactivate: `n8n update:workflow --id=<id> --active=true`
5. `n8n publish:workflow --id=<id>` also activates, with the same caveat.
6. **Restart n8n.** Per n8n's own CLI docs: these commands write directly to
   the database, and "if you execute them while n8n is running, the changes
   don't take effect until you restart n8n." On a non-multi-main instance, a
   previously-active workflow's cron triggers even keep running post-import
   until you restart — CLI-imported/activated changes do **not** take effect
   live. This is the main disadvantage vs. Method A.
7. To inspect what actually happened without MCP: query
   `execution_entity` (id/status/finished) and `execution_data` (full JSON
   run detail, incl. `lastNodeExecuted` and error stack) directly in the
   same sqlite database.
8. To find the *actual* `typeVersion`/credential schema a node supports on
   **this** n8n version (not whatever n8n-mcp's static catalog assumes,
   which reflects the newest n8n release, not what's installed): read the
   installed source directly —
   `.../node_modules/n8n/node_modules/n8n-nodes-base/dist/nodes/<Node>/<Node>.node.js`
   (`defaultVersion` field) and
   `dist/credentials/<Type>.credentials.js` (`properties`).
9. Import with `httpHeaderAuth` (or similar) credentials via n8n-mcp's
   credential create/update sometimes validates against an unrelated schema
   requiring `allowedDomains` — include a dummy `"allowedDomains": ""` in the
   `data` payload to work around it, even for credential types that don't
   logically have that field.
10. A Code node's `jsCode` that needs its own template-literal `${...}`
    interpolation shouldn't be nested inside the patch script's own backtick
    string — backtick-escaping across two nesting levels is error-prone.
    Write the Code node's JS to a standalone `.js` file and
    `fs.readFileSync` it into `parameters.jsCode` instead.

---

## 4. Errors hit and how they were actually fixed

Grouped by category — this is the part most reusable for the next project.

### Node version / catalog mismatches

- **Symptom**: a node's parameters render **empty** in the editor, or it
  throws `Cannot read properties of undefined (reading 'execute')` on run.
- **Cause**: n8n-mcp's static catalog reflects the *newest* n8n release, not
  what's actually installed. It confidently suggests `typeVersion`s the
  installed instance doesn't register.
- **Fix**: run `n8n_audit_instance` (categories `["instance"]`) *first*, on
  every new project, to learn the real installed version and how stale the
  catalog might be. When a node shows `isVersioned: true` with multiple
  versions, don't default to "current" — pick one or two minors behind
  latest unless you've confirmed the instance supports the newest. Concrete
  example from this project: Form Trigger's catalog-"current" `2.6` silently
  loaded empty; `2.2` worked and had everything needed.

### Community nodes the catalog doesn't index

- **Symptom**: `get_node`/`validate_node` return empty `properties`/
  `credentials` arrays, or `getSchema` 404s, for a community node (here:
  `n8n-nodes-resend.resend`).
- **Fix**: don't trust the "missing property" warnings as authoritative —
  they're catalog gaps, not real errors. Read the node's actual source on
  GitHub (e.g. `raw.githubusercontent.com/<org>/<pkg>/master/nodes/.../*.ts`)
  for exact parameter names, `displayOptions` gating, and the credential
  type name. This is how the Resend node's `authentication: "apiKey"` field
  (easy to miss — gates the credential behind a `displayOptions.show`) and
  the `resendApi` credential shape were confirmed.

### Postgres node behavior surprises

- **0 rows silently kills the downstream branch** — no error, just an empty
  200 response. Any SELECT that can legitimately return 0 rows on the happy
  path (dedupe lookup, cache lookup, specialist pick) needs
  `alwaysOutputData: true` on that node **if** something downstream (e.g. a
  merge point other branches also feed into) must still run. If 0 rows
  should just legitimately end that branch (e.g. "no follow-ups due"),
  letting it stop naturally is correct — don't reach for
  `alwaysOutputData: true` reflexively.
- **`update` operation (v2.2+) uses a different parameter shape than
  `insert`**: `columns.matchingColumns: [...]` + `columns.value: {...}`
  (the match column's value must *also* be included in `value`) — **not**
  `columnToMatchOn`/`valueToMatchOn`. Those older-style params are silently
  ignored on v2.2+, and the node throws `"Could not get parameter
  columns.matchingColumns"`.
- **After any Postgres INSERT/UPDATE in a chain, a downstream node's bare
  `$json` is that node's own returned row**, not your original data (e.g. a
  `lead_activity` insert's own `id`, not the lead's `id`). Once a chain
  passes through a side-effecting DB write, always use named
  `$('Node Name').first().json.field` references, never bare `$json`, for
  data from further back in the chain.

### Code node mode/shape mismatches

- **Return shape depends on execution mode**: `runOnceForEachItem` must
  `return {json:{...}}` (bare object); `runOnceForAllItems` must
  `return [{json:{...}}]` (array). Wrapping the wrong way throws
  `"A 'json' property isn't an object"`.
- **Can't skip an item with `return null`/`undefined`** in
  `runOnceForEachItem` mode — throws a hard validation error inside n8n's
  sandbox, not a catchable one. To filter items, switch to
  `runOnceForAllItems` and do a full array filter (`$input.all()` → filter →
  return the surviving array).

### Expression parser

- **Don't embed a large JSON object (e.g. a strict JSON schema) directly
  inside an `={{ }}` expression** — breaks the parser with a bare "invalid
  syntax" error and no line number. Build the payload in a preceding Code
  node instead and reference it with a trivial expression like
  `={{ $json.requestBody }}`.

### Webhook response mode

- **`responseMode: "lastNode"` breaks when a workflow has multiple
  independent terminal branches** that don't converge (e.g. a stale-sweep
  branch and a follow-up-processing branch) — throws `"No item to return was
  found"` at the HTTP layer even though the workflow itself ran fine, because
  n8n can't unambiguously pick which branch's last item to return. For any
  webhook whose only job is to kick off a multi-branch background process
  (manually-triggerable crons, fan-out workflows), use
  `responseMode: "onReceived"` (immediate ack, ignores workflow output)
  instead.

### Silent failures masked by error handling

- **A Resend/community node with `onError: continueRegularOutput` can hide a
  total send failure indefinitely.** Concrete bug found this way: WF-1's
  "Notify Specialist" node had **no `credentials` block at all** — it had
  been silently failing on every single run since the workflow was built,
  and nothing ever surfaced because of the `onError` setting. Lesson: when a
  workflow "runs green" end to end, that doesn't prove every node actually
  did its job — spot-check that credentials are actually attached on any
  node with `continueRegularOutput`, don't just trust the green checkmark.
- **Fictional/placeholder email domains bounce silently in Resend** (a
  `specialists.email` column seeded with `@cyberlead.demo` — no real mail
  server exists for that domain). If a "notify" step never seems to arrive,
  check whether the destination address is even real before debugging the
  workflow logic.

### Application-level "looks like a bug but isn't"

- **Dedup logic that keys on email only** (not company/contact name) will
  make a resubmission with a different fake company name but the same email
  look like nothing happened: n8n returns webhook success, but there's no
  new row and no notify email, because it correctly matched an existing lead
  and just bumped a submission counter. Before treating "success but nothing
  new appeared" as a bug, check whether a dedup/idempotency guard fired as
  designed — walk the actual execution's node outputs
  (`n8n_executions` → `action: "get"`, `mode: "preview"` or `"filtered"`)
  rather than guessing.

### Frontend/infra edge cases encountered while wiring this up

- **`pg-connection-string` deprecation warning surfaced as a Next.js dev
  "Console Error"**: a `DATABASE_URL` with `sslmode=require` (or `prefer`/
  `verify-ca`) triggers a `console.warn` about those modes being deprecated
  aliases for `verify-full`. Next's dev overlay treats server-side warnings
  during render as errors. Fix: use `sslmode=verify-full` explicitly — same
  actual TLS behavior, just silences the noisy warning. Needs a dev-server
  restart to pick up the `.env.local` change.
- **`.env.local` changes never take effect on a running `next dev` process**
  — always kill and restart it after editing env vars (see §2).
- **n8n webhook credential secrets are never retrievable via the n8n API**
  once set — if you need the frontend to call an n8n webhook with a shared
  secret, create a fresh `httpHeaderAuth` credential with a *known* value up
  front and store that same value in the frontend's `.env.local`; don't plan
  to read it back out later.

---

## 5. How each workflow was actually verified (not just "looked done")

"The workflow validated with no errors" was never treated as sufficient on
its own. The pattern that worked:

1. `n8n_validate_workflow` (`profile: "runtime"`) after every edit — catches
   structural/expression errors before a real run.
2. Trigger a real execution (webhook curl, form submission, or the
   workflow's own manual-trigger webhook for crons) and pull the execution
   back with `n8n_executions` (`action: "get"`, `mode: "preview"` to see
   which nodes ran and their output shape, or `mode: "filtered"` with
   specific `nodeNames` to inspect one node's actual data in full).
3. Cross-check the *actual effect* outside n8n — query Postgres directly for
   the row that should have been created/updated, check the real inbox/
   Resend dashboard for the email that should have sent. n8n reporting
   `"status": "success"` only proves no node threw; it doesn't prove the
   row exists or the email arrived (see the dedup false-alarm above, and the
   missing-credentials bug that ran "green" for an unknown number of prior
   executions).
4. For anything gated behind a **real external service** (Resend inbound
   webhooks, real domain sending), do at least one fully real end-to-end
   test — a genuine Svix-signed webhook arriving through ngrok, a genuine
   email delivered to a real inbox — not just a synthetic n8n "manual
   execute". This is how the WF-1 missing-credentials bug and the
   ngrok-URL-staleness failure mode were actually caught; a synthetic test
   inside n8n would not have surfaced either.
5. Clean up test data immediately after verifying — delete test rows via
   direct SQL (respecting FK order: child tables before the parent `leads`
   row) rather than leaving synthetic leads in the dashboard.

---

## 6. Reusable pattern: admin tasks against a third-party API without exposing the raw key

Several one-off admin tasks were needed against Resend's API (register/list/
delete webhooks, add/verify a sending domain) without ever putting the raw
API key in a chat message or a throwaway script. The pattern used every time:

Build a small, disposable (or standing) n8n workflow with a `GET` webhook
trigger that calls the third-party API using the **existing n8n credential**
(already stored securely in n8n, never re-entered). Then just `curl` that
n8n webhook URL. n8n holds the secret; the terminal/chat never sees it.

This project ended up promoting the Resend-webhook-admin version of this
pattern into a **standing** workflow (`SETUP - Resend Webhook Admin`) instead
of rebuilding a disposable one every time ngrok's URL changed — worth doing
for any admin task you expect to repeat more than once or twice.

---

## 7. General checklist for the *next* n8n project

1. Run `n8n_audit_instance` first thing, before picking any node — learn the
   real installed version.
2. Prefer n8n-mcp's REST API tools (`n8n_update_partial_workflow`, etc.) over
   CLI/sqlite editing whenever they're available — no restart needed, faster
   iteration. Only fall back to CLI+sqlite if MCP truly has no API access.
3. For any node with multiple `typeVersion`s, default to "a version or two
   behind latest, confirmed against this instance" — not the catalog's
   "current".
4. For community nodes, treat sparse/empty n8n-mcp catalog output as
   "unknown", not "this node has no such property" — verify against the
   package's real source before concluding a field doesn't exist.
5. Always run `n8n_validate_workflow` after edits, but verify with a real
   execution + a check of the actual downstream effect (DB row, email,
   whatever) before calling something done.
6. Any node with `onError: continueRegularOutput` needs an extra trust
   check — confirm it actually has working credentials/config, since a
   silent failure there is, by design, invisible.
7. Any workflow triggered by a real external webhook (not just n8n-internal)
   needs a plan for what happens when the local tunnel URL changes — either
   a stable public host, or a documented one-command re-registration step.
8. Keep an admin-task workflow pattern (§6) ready for any third-party API
   you'll need to poke more than once, instead of pasting raw keys into
   scripts or chat.

---

## Sources

CLI-import/restart behavior in §3 (Method B) confirmed against n8n's own docs:

- [Use the command line — n8n Docs](https://docs.n8n.io/deploy/host-n8n/configure-n8n/use-the-command-line)
- [CLI commands — n8n Docs](https://docs.n8n.io/hosting/cli-commands/)
