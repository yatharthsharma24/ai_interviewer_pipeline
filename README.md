# Interview Pipeline

**Part 1 — sourcing & screening.** The admin configures a role once. The agent builds the
Google Form, emails the link out, pulls every response into a database, throws away the
incomplete ones, and ranks the rest against the role using a local open-source model.

**Part 2 — interview rounds.** The admin sets an acceptable score; the agent rejects everyone
below it, gives the rest interview slots, and notifies them by email.

**Part 3 — the AI voice interview.** Each candidate opens a personal link at their slot time
and is interviewed by a voice agent that has read their resume. The call is transcribed and
proctored, then graded into a rating and a written report.

```
   admin config ──► Google Form ──► applicants
                                        │
                                        ▼
                              responses (Forms API)
                                        │
                                        ▼
                              normalise into SQLite
                                        │
                    ┌───────────────────┼───────────────────┐
                    ▼                   ▼                   ▼
            blank required        fails hard rules      survivors
            fields                (experience,               │
                    │              must-have skills,         ▼
                    │              notice, budget)      LLM fit score
                    ▼                   ▼                    │
            rejected_incomplete   rejected_rules    ┌─────────┴─────────┐
                                                    ▼                   ▼
                                              shortlisted        rejected_score
                                                    │
  ── PART 2 ────────────────────────────────────────┼────────────────────────────
                                                    ▼
                                          admin's acceptable score
                                                    │
                                        ┌───────────┴───────────┐
                                        ▼                       ▼
                                 rejected_round            slot assigned
                                 (nothing sent)                 │
                                                                ▼
                                                       email invitation
                                                                │
                                                                ▼
                                                     scheduled ──► Part 3
```

Every rejection carries its reasons, so an admin can audit or override any decision.

## Stack

| Piece | Choice |
|---|---|
| Backend | FastAPI + a Typer CLI + an admin dashboard, all over the same code |
| Database | SQLite via SQLAlchemy — swap `DATABASE_URL` for Postgres, no code change |
| Forms | Google Forms API (creates the form *and* reads responses) |
| Email | Gmail API |
| Screening | deterministic rules first, then an LLM — **Ollama by default** (local, free), OpenAI optional |
| Voice interview | OpenAI Realtime (WebRTC) with automatic **Gemini Live** failover, in-browser proctoring (MediaPipe) |
| Messaging | Gmail API (email), pluggable, dry-run by default |

---

## Setup

```bash
cd interview_pipline
python -m venv .venv
.venv\Scripts\activate          # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt

copy .env.example .env          # cp on macOS/Linux — defaults work as-is
python -m app.cli init
```

Scoring runs locally through [Ollama](https://ollama.com) by default — no API key, no cost,
and candidate resumes never leave your machine. Install it, pull a model, and confirm:

```bash
ollama pull llama3              # or gemma4 / mistral / gemma3 / phi3
python -m app.cli backend       # shows the active backend and sends a test prompt
```

To use hosted OpenAI instead, set `SCREENING_PROVIDER=openai` and `OPENAI_API_KEY` in
`.env`. See [Choosing a scoring backend](#choosing-a-scoring-backend).

Google access is a one-time console setup — follow **[docs/GOOGLE_SETUP.md](docs/GOOGLE_SETUP.md)**,
drop the OAuth client JSON at `secrets/credentials.json`, then:

```bash
python -m app.cli google-login
```

Verify the offline half of the system at any time (no Google, no API key, no cost):

```bash
python scripts/smoke_test.py
```

---

## Everyday use

```bash
python -m app.cli new-job          # interactive wizard — asks for the whole role spec
python -m app.cli create-form 1    # builds the Google Form, prints the share link
python -m app.cli send-link 1 --to alice@example.com --to bob@example.com
python -m app.cli run 1            # sync responses + screen them
python -m app.cli shortlist 1
python -m app.cli candidate 7      # full profile + why it scored that way
python -m app.cli export 1

python -m app.cli round-new 1      # Part 2: acceptable score, date, slot length
python -m app.cli schedule 2 --apply
python -m app.cli interview-prepare 2   # Part 3: plan questions, mint join links
python -m app.cli notify 2         # dry run by default; --send to go live
```

Or do all of it in the browser — see [the admin dashboard](#the-admin-dashboard).

Run `run` on a schedule (Task Scheduler / cron) and the pipeline maintains itself — syncing
is idempotent, keyed on Google's `responseId`.

The API is the same feature set:

```bash
python -m app.cli serve           # http://127.0.0.1:8000/docs
```

| Endpoint | Purpose |
|---|---|
| `POST /jobs`, `GET/PATCH/DELETE /jobs/{id}` | job configuration |
| `POST /jobs/{id}/form` | generate the Google Form |
| `POST /jobs/{id}/form/link` | adopt a form the admin built by hand |
| `POST /jobs/{id}/form/send` | email the link |
| `POST /jobs/{id}/sync` | pull responses into the DB |
| `POST /jobs/{id}/screen` | filter + score |
| `POST /jobs/{id}/run` | sync then screen |
| `GET /jobs/{id}/candidates` | everyone, filterable by status/score |
| `GET /jobs/{id}/shortlist` | the Part 1 → Part 2 handoff |
| `GET /fields` | the canonical field vocabulary |
| `POST /jobs/{id}/rounds` | create an interview round |
| `POST /rounds/{id}/schedule` | apply the score bar + allocate slots |
| `POST /rounds/{id}/notify` | send the invitations |
| `GET /rounds/{id}/slots` | the Part 2 → Part 3 handoff |
| `GET /interview/{token}` | the candidate's call page |
| `GET /rounds/{id}/interviews` | interview status for a round |
| `GET /interviews/{id}` | transcript, proctoring events, and report |
| `POST /interviews/{id}/grade` | write the summary and ratings |

---

## The admin dashboard

```bash
python -m app.cli serve      # then open http://127.0.0.1:8000/admin
```

Everything the CLI does, in a browser. It drives the **same JSON API** the CLI drives, so
there is no second implementation of the pipeline logic that can drift from the tested one.
No build step — plain HTML, CSS and JavaScript served by FastAPI.

| Page | What you can do |
|---|---|
| **Overview** | Every job with its funnel counts; anything unhealthy surfaces at the top |
| **System check** | Component-by-component health, with the fix for each problem |
| **Access** | Set `ADMIN_PASSWORD` in `.env` to require a login (mandatory once public) |
| **Job** | Funnel chart, generate/link the form, email it, sync, screen, delete |
| **Candidates** | Sortable table filtered by status; click through to the full verdict |
| **Candidate** | Profile, rationale, strengths, concerns, and any evidence-cap explanation |
| **Rounds** | Create a round; set the bar, the dates, and the interview difficulty |
| **Round** | Preview → apply the schedule, prepare AI interviews, dry-run → send |
| **Interview** | Question plan, transcript, proctoring events, and the graded report |

Want data to look at first?

```bash
python scripts/demo_seed.py          # one job, 7 candidates across every funnel state
python scripts/demo_seed.py --clean  # remove it again
```

### System check

The dashboard's System page (`GET /system/status`) answers "is anything broken?" for all
every component: both databases, the screening model, OpenAI, Gemini, Google, whether
messages are live or dry-run, and whether the interview URL is actually reachable by a
candidate. Each problem comes with the specific fix.

Two depths: the default is configuration-only and instant; **Run deep check** additionally
makes live calls (Ollama tags, OpenAI models, a Google token refresh). The Google check
never triggers the OAuth consent flow — a status page that pops a browser window on an
unattended server would be worse than no status page.

---

## Verifying the whole thing

```bash
python scripts/smoke_test.py    # 128 unit checks — no HTTP, no credentials, no cost
python scripts/verify_all.py    # 61 end-to-end checks against a live in-process server
python scripts/verify_all.py --online   # also exercises OpenAI (a few cents)
```

`verify_all.py` drives the real FastAPI app the way the dashboard does — real routes, real
database writes, real serialisation — using throwaway databases so it never touches your
data. Anything needing outside credentials is reported as **SKIP with the reason**, never
quietly passed.

---

## What the admin configures up front

Collected by `new-job`, or `POST /jobs`:

**Role** — title, department, location, employment type, description, responsibilities.

**Hard requirements** — minimum (and optional maximum) years of experience, must-have
technologies, nice-to-have technologies, education, maximum notice period, compensation
ceiling.

**Screening** — how hard the bar is, which questions are mandatory, and any free-text
guidance to hand the scorer (`screening_notes`).

### Strictness

One setting moves every threshold together:

| | score cutoff | must-have skills needed | experience tolerance | verifiable link required |
|---|---|---|---|---|
| `lenient` | 45 | 34% | ±2.0 yrs | no |
| `balanced` | 62 | 60% | ±1.0 yrs | no |
| `strict` | 76 | 85% | ±0.5 yrs | yes |
| `very_strict` | 87 | 100% | none | yes |

Defined in `STRICTNESS_PROFILES` (`app/models.py`) — tune the numbers there.

### Candidate fields

Fifteen canonical fields (`python -m app.cli fields`) drive three things at once: which
questions the generated form asks, how answers map back to database columns, and which
blanks count as disqualifying. Mandatory by default: name, email, mobile, years of
experience, skills, resume link.

---

## How screening works

**Stage 1 — completeness.** Any mandatory field left blank rejects the response. Email,
phone, and URL fields are also format-checked, so `not-an-email` is treated as missing
rather than passed downstream.

**Stage 2 — hard rules.** Experience floor and ceiling, must-have skill coverage, notice
period, compensation. Skills are matched across the whole profile, not just the checkbox
question — "shipped Django services on PostgreSQL" counts.

**Stage 3 — LLM scoring.** Only survivors reach the model, so cost and time track the
*qualified* pool rather than the raw one. The model returns a structured verdict — score,
recommendation, matched/missing skills, strengths, concerns, seniority read, and a written
rationale — all persisted on the candidate row. Above the strictness cutoff → `shortlisted`.

Three deliberate choices in `app/screening/llm.py`:

- The job spec sits first in the system message and the candidate profile last. That lets a
  hosted provider reuse a cached prefix, and lets Ollama keep the model warm across a batch
  — don't reorder those two.
- The scoring prompt tells the model that missing information is not evidence of a
  deficiency, and to stay calibrated. Inflated scores make a shortlist useless.
- **`recommendation` is derived from `fit_score`, not trusted from the model.** Benchmarking
  caught two local models returning `strong_yes` next to a score of 25. `fit_score` is what
  the cutoff compares against, so the label is computed from it and the model's original
  answer is kept in `assessment.model_recommendation` — a wide disagreement there is a
  useful low-confidence signal.

### The evidence gate

Benchmarking found that **every** local model shortlisted a keyword-stuffed application —
one that lists all the right technologies with no account of what the candidate actually
built. Each model's own rationale said the detail was missing, then scored it 65–85 anyway.

Two prompt fixes failed. "Score it no higher than 49" was ignored. A three-way
`specific`/`partial`/`unsupported` grade collapsed to `partial` as a comfortable middle.

What works is splitting the judgement from the arithmetic. The model answers one *factual*
question about the text — `describes_concrete_work`: does the application name a single
thing this person built, decided, fixed, or measured? — and the cap is applied in code
(`UNSUPPORTED_EVIDENCE_CAP`, default 49). Small models are reliable at that yes/no and
unreliable at obeying a numeric ceiling.

The pre-cap score is preserved in `assessment.model_fit_score` and a line is appended to
`concerns`, so a capped candidate is auditable rather than silently downgraded.

This is the general pattern in `llm.py`: **ask the model for judgement, do the arithmetic in
code.** Same reason `recommendation` is derived rather than trusted.

Output is **schema-constrained, not requested**. Ollama compiles the JSON schema into a
decoding grammar, so the reply is valid JSON by construction; OpenAI does the same through
strict `json_schema`. Neither path relies on the model choosing to obey "reply in JSON".

`--no-llm` runs stages 1 and 2 only and shortlists everyone who clears the hard bar — useful
for testing the plumbing, or if Ollama isn't running.

---

## Part 2 — interview rounds

Part 1 hands over a shortlist. Part 2 applies the admin's **acceptable score**, gives the
survivors interview slots, and tells them — by email.

```bash
python -m app.cli round-new 1        # acceptable score, date, slot length, meeting link
python -m app.cli schedule 2         # preview: who is in, who is out, what the timetable looks like
python -m app.cli schedule 2 --apply # persist the schedule
python -m app.cli notify 2           # send (dry run by default)
python -m app.cli round-show 2       # timetable + delivery status
```

### The score bar

The admin sets `acceptable_score` per round. Candidates below it become `rejected_round`;
the rest get a slot. This is a *second, tighter* gate on top of Part 1 — it only ever looks
at candidates Part 1 already shortlisted, so someone rejected for missing a must-have skill
is never resurrected by a score threshold.

Candidates with **no** fit score (screened with `--no-llm`) are held out rather than judged
against a numeric bar, and reported for the admin to decide.

### Slot allocation

Give it a date, a daily window, and a slot length; it lays candidates out in score order,
rolls over to the next day when a day fills, and skips weekends unless told not to.

```
Round: Technical Round 1        Date: 2026-08-14   10:00–17:00   30 min slots

  Asha Rao      Fri 14 Aug 10:00–10:30
  Ravi Kumar    Fri 14 Aug 10:30–11:00
  ...
  Priya Nair    Mon 17 Aug 10:00–10:30     ← weekend skipped
```

`schedule` previews by default and only writes with `--apply`, so the whole timetable can be
reviewed before anything is committed — and nothing is sent at this stage either.

**Times are stored in UTC and rendered in the round's timezone.** Slots are computed in local
wall-clock time first, so "10:00" stays 10:00 across a DST change instead of drifting.

### Notifications

Invitations go out by email (Gmail API), carrying the full detail: the time in the round's
timezone, the duration, and the candidate's own interview link.

The channel layer in `app/notify/channels.py` is an interface with swappable
implementations, so another transport can be added without touching the scheduling logic.

`NOTIFY_DRY_RUN=true` is the default and prints messages instead of sending them. Turn it off
with `--send`, which also asks for confirmation naming how many real people are about to be
messaged.

| Behaviour | Why |
|---|---|
| One failure never aborts the run | A bad email address must not stop the other 49 invitations |
| Failures recorded per candidate | `round-show` lists exactly who failed and why |
| Re-running only retries failures | Nobody gets a duplicate invitation |
| A missing email is a skip, not a crash | Reported, and the run carries on |

### Rejections

Candidates below the bar are marked `rejected_round` and **nothing is sent**. The status is
recorded so the decision is auditable, but an accidental mass rejection email is not
recoverable. A rejection template exists in `app/notify/templates.py` if you want to wire it
up deliberately.

### What Part 3 reads

```bash
GET /rounds/{round_id}/slots
```

Each slot carries the confirmed UTC start and end, the delivery audit trail, and the full
embedded candidate — everything an interview agent needs.

---

## Part 3 — the AI voice interview

The candidate opens a personal link at their slot time and is interviewed by a voice agent
that has read their resume. The call is transcribed, proctored, graded, and stored.

```bash
python -m app.cli interview-prepare 2 --difficulty hard   # plan questions, mint join links
python -m app.cli notify 2 --send                         # links go out with the invitation
python -m app.cli serve                                   # candidate opens their link
python -m app.cli interviews 2                            # status of everyone in the round
python -m app.cli interview-grade --round-id 2            # write summaries and ratings
python -m app.cli interview-show 7                        # transcript + full report
```

### How the call works

Speech-to-speech through OpenAI's Realtime API (`gpt-realtime-2.1-mini` by default), over
WebRTC straight from the browser — anything slower than that feels wrong in an interview.

**The browser never sees your API key.** It asks our server, which exchanges the key for a
short-lived client secret scoped to one session:

```
browser --ask-->        our server  --API key-->  POST /v1/realtime/client_secrets
browser <--token--      our server                (ephemeral, single session)
browser --SDP + ephemeral token-------------->    OpenAI
```

### Failover: Gemini as the alternative

Set `GEMINI_API_KEY` and **every** model call in Part 3 — planning the questions, the live
call itself, and grading the transcript — falls back to Gemini when OpenAI does not answer.
A missing key, a rotated one, a 429 on an exhausted quota and a connection reset all look
identical from the candidate's side (the interview does not happen), so the fallback triggers
on any failure rather than only on an outage.

```
python -m app.cli interview-backend           # who runs what, and is there a fallback
python -m app.cli interview-backend --probe   # really call each one, including the live socket
```

`--probe` exercises both paths for both providers: a structured-output request for the text
path, and a real WebSocket handshake for the voice path. Do this before a hiring round —
it is the only thing that proves the fallback is actually there.

`INTERVIEW_PROVIDER=gemini` makes Gemini primary without deleting the OpenAI key.

**On model names.** The defaults are the `-latest` aliases, deliberately. Google retires a
model for *new* API keys while continuing to list it and continuing to serve it to existing
ones — so a pinned `GEMINI_MODEL` can start answering

> This model is no longer available to new users.

while still appearing in `/v1beta/models`. Every check built on "is it in the model list?"
says yes right up until a finished interview cannot be graded. The status check and
`verify_all --online` therefore make a real one-token generation call instead of reading the
listing. Pin a version only if you need grading held constant across a round, and re-probe
before each one.

The live call is the one place the two are not interchangeable, because the transports have
nothing in common. OpenAI Realtime is WebRTC and the browser connects to OpenAI directly.
Gemini Live is a WebSocket carrying raw PCM, and it authenticates with the API key in the
connection URL — with no per-session client secret to hand out. Sending the browser the real
key is not an option, so on this path the server **relays** the socket:

```
browser <--ws--> our server <--ws + API key--> Gemini Live
                 (candidate's interview token authorises the browser side)
```

Relaying costs one extra hop of latency and buys two things: the key never leaves the server,
and the transcript is captured from the frames the *server* sees. On the OpenAI path the page
posts its own transcript back to us, so a doctored page could lie about what was said; on the
Gemini path it cannot.

Failover is silent to the candidate, which means it would also be invisible to you — so each
interview records which provider served each stage (`providers` on the interview record,
shown in the dashboard as "ran on …"). Two candidates graded by different models are not
strictly comparable, and that should not be a hidden fact.

### Questions are planned before the call

The plan is generated once, up front, from the job spec, the candidate's application, their
resume text where it could be fetched, and **Part 1's `concerns`** — the screener already
wrote down what a recruiter should dig into for this person.

Planning ahead means the plan can be reviewed before a candidate ever sees it, the realtime
model spends its budget on conversation rather than planning, and two candidates for one role
get comparably structured interviews. It is a spine, not a script: the interviewer follows up
freely within each topic.

`--difficulty` is set per round — `easy` / `medium` / `hard` / `expert` — controlling how many
questions are asked and how hard the interviewer pushes. It is deliberately **separate** from
Part 1's screening strictness: you might screen hard and interview gently, or the reverse.

### Loading the right resume

The one failure this system must never have is interviewing someone against another
candidate's resume, so identity is re-checked on **every** load of an interview:

| check | on failure |
|---|---|
| the join token exists | 404, generic message |
| the candidate still exists | refuse |
| the stored dossier's `candidate_id` matches | refuse |
| the stored email still matches the candidate | refuse |

A mismatch is logged loudly server-side and shown to the candidate as a neutral "could not be
verified" — they cannot fix it and should not be told whose record it collided with.

Resume text is fetched where possible (direct PDFs, and Drive/Docs share links get rewritten
to a download URL). **Most Drive links will not fetch** — this app's OAuth scope only covers
files it created — so a failure is recorded rather than ignored, and the interviewer falls
back to the structured application profile, which is itself a complete dossier.

### Proctoring

Runs entirely in the browser. **Video is never uploaded** — only violation events are.

| signal | how |
|---|---|
| tab switched away | Page Visibility API |
| window lost focus | `blur` / `focus` |
| looking away | MediaPipe face landmarks — head yaw/pitch plus iris offset |
| face missing | landmark detection finds no face |
| more than one person | landmark detection finds two faces |

Every signal is **duration-based**. A glance away is not cheating, so a violation is only
counted after it is sustained past a threshold; briefer observations are stored with
`counted=false` as context for a reviewer but never held against the candidate. After
`PROCTOR_MAX_VIOLATIONS` counted violations the call ends itself.

If MediaPipe fails to load, gaze detection degrades to unavailable and **says so** on the
record rather than silently reporting a compliant candidate. Tab monitoring keeps working.

### Transcript and grading

Transcripts, ratings and proctoring events live in a **separate database**
(`INTERVIEW_DATABASE_URL`, default `data/interviews.db`) from the candidate/resume database.
Interview recordings are far more sensitive than an application form, and the split makes
them separately backed up, encrypted, and deletable under a retention policy. The cost is
that SQLite cannot enforce a foreign key across files, so the link is validated in code —
see the identity checks above.

Grading runs **after** the call on the stored transcript, using a normal text model rather
than the realtime one. The interviewer is deliberately not the grader: an interviewer that
grades its own conversation scores it kindly, and the candidate can hear evaluation leaking
into the questions. Separating them also means grading can be re-run or tuned without
touching a recording.

The report gives an overall rating, a hire recommendation, per-competency scores with
evidence quoted from the transcript, per-question notes, strengths, concerns, red flags, and
a communication read. A competency that never came up is marked `not_demonstrated` rather
than guessed at from the resume.

### Letting real candidates join

By default the join link is `http://127.0.0.1:8000/...`, which only works on your machine.
Candidates need a **public https address** — https specifically, because browsers only grant
camera and microphone access in a secure context. A public `http://` link loads the page and
then silently fails to start the call.

Fastest route, free and no account:

```powershell
winget install --id Cloudflare.cloudflared     # once
cloudflared tunnel --url http://localhost:8000 # leave running; prints an https URL
python -m app.cli serve --public-url https://<that-url>
```

Then `interview-prepare` → `notify --send`, in that order, so the invitation carries the
public link.

**Set `ADMIN_PASSWORD` in `.env` first.** Exposing the server exposes the dashboard too, and
the server refuses to start publicly without it. Candidate links are unaffected — they are
authorised by their own secret token.

The free tunnel gets a **new URL every restart**, so links already emailed stop working. Fine
for sending and interviewing in one sitting; for anything longer you need a named tunnel or a
deployment. Full walkthrough and troubleshooting: **[docs/GOING_PUBLIC.md](docs/GOING_PUBLIC.md)**.

### Cost

Realtime audio is billed per minute **in both directions** — a 30-minute interview is not
free. `gpt-realtime-2.1-mini` is the default for that reason; switch `REALTIME_MODEL` to
`gpt-realtime-2.1` for more natural follow-ups at higher cost.

---

## Choosing a scoring backend

Set `SCREENING_PROVIDER` in `.env`.

|  | `ollama` (default) | `openai` |
|---|---|---|
| Cost | free | per token |
| Privacy | resumes never leave the machine | sent to the provider |
| Setup | install Ollama, pull a model | paid API key |
| Quality | varies by model — benchmark it | consistently strong |

Check the active backend and that it actually responds:

```bash
python -m app.cli backend
```

### Ollama

```bash
ollama pull llama3          # or gemma4, mistral, gemma3, phi3
python -m app.cli backend
```

Local models differ a lot at this task, so **measure rather than guess**:

```bash
python scripts/bench_models.py
```

That runs six fabricated candidates — spanning excellent, decent, junior, wrong-stack,
keyword-stuffer, and a genuinely borderline case — through every installed model.

The metric is **shortlist decision accuracy**: does the model's score put each candidate on
the correct side of the cutoff? That is the only thing the pipeline does with the number.
A model that ranks candidates perfectly but shortlists an unqualified one is worse than one
that ranks them sloppily and still calls every shortlist right. False positives are treated
as worse than false negatives — a bad shortlist wastes interview time in Part 3, while a
missed candidate is recoverable by loosening strictness.

Six hand-written candidates is a smoke benchmark, not a rigorous eval. It tells you a model
is not obviously broken. Re-run it against your own applicant pool once you have one.

A full sweep takes ~15 minutes. When you change the prompt to fix one specific failure,
re-test just that case first:

```bash
python scripts/bench_case.py "keyword stuffer"
python scripts/bench_case.py "junior" gemma4:latest      # or pin the models
```

It prints each model's score, whether the evidence cap fired, and the rationale — enough to
tell whether a prompt change actually landed. Always confirm with a full sweep before
keeping it: a tweak that fixes one case can quietly regress another.

### Measured results

Run on this machine (RTX-class GPU, Ollama 0.30.7), before and after the evidence gate:

| model | size | correct before | **correct after** | false positives | speed |
|---|---|---|---|---|---|
| **gemma4** | 8B | 5/6 | **6/6** | 0 | 25.2s |
| phi3 | 3.8B | 4/6 | 5/6 | 1 | 6.1s |
| mistral | 7B | 4/6 | 5/6 | 1 | 7.8s |
| gemma3 | 4B | 3/6 | 4/6 | 0 | 8.7s |
| llama3 | 8B | 4/6 | 4/6 | 2 | 6.7s |

`gemma4:latest` is the default: the only model that got every decision right, with no false
positives. It is ~4x slower than the small models — roughly 40 minutes per 100 candidates
versus 10 — but screening runs on a schedule, not interactively, and only candidates that
already cleared the hard rules ever reach it. If throughput matters more than precision,
`phi3` is the fast alternative at 5/6.

Speed figures are warm — the first call after an idle period also pays model load. That is
what `OLLAMA_KEEP_ALIVE` is for.

`OLLAMA_NUM_CTX` (default 8192) is the setting that matters most. Ollama's own default
context window is small enough that a long job spec plus a long candidate profile gets
silently truncated — the model then scores on a partial prompt and nothing tells you.
Raise it for verbose specs.

### OpenAI

```bash
SCREENING_PROVIDER=openai
OPENAI_API_KEY=sk-...
SCREENING_MODEL=gpt-5
```

`SCREENING_EFFORT` (`minimal`/`low`/`medium`/`high`) maps to `reasoning_effort` and is applied
only on reasoning models (gpt-5, o-series); on a gpt-4-class model it is dropped rather than
erroring. `OPENAI_BASE_URL` points the client at Azure OpenAI or any compatible gateway.

Adding a third provider means implementing one method — `complete_json(system, user, schema)`
in `app/screening/backends.py`. Nothing in the screening logic knows which backend is live.

---

## Layout

```
app/
  config.py          settings from .env
  models.py          JobOpening, Candidate, strictness profiles
  field_map.py       the 15 canonical fields + fuzzy title matching
  parsing.py         "4.5 yrs" -> 4.5, "12 LPA" -> 1200000, "Immediate" -> 0
  schemas.py         API request/response models
  google_api/
    auth.py          OAuth flow and token cache
    forms.py         create form, adopt form, read responses
    gmail.py         send the link
  migrate.py         additive column migrations, shared by both databases
  scheduling.py      slot allocation — pure, no I/O  (part 2)
  screening/
    rules.py         completeness + hard rules  (stages 1-2)
    llm.py           schema, prompt, parsing    (stage 3)
    backends.py      Ollama / OpenAI transports
    pipeline.py      sync + orchestration
  notify/
    channels.py      email / dry-run transports
    templates.py     invitation bodies
    rounds.py        score bar, allocation, sending
  interview/         (part 3)
    models.py        Interview / TranscriptTurn / ProctoringEvent - separate DB
    db.py            its own engine and session factory
    resume.py        dossier building + resume fetching
    plan.py          question plan generation
    providers.py     OpenAI/Gemini text providers + automatic failover
    realtime.py      live session setup for both providers
    live_proxy.py    WebSocket relay to Gemini Live (keeps the key server-side)
    grading.py       post-call summary and ratings
    service.py       identity-checked orchestration
    web.py           call page, transcript/event ingest, admin reads
    static/call.html the candidate's browser page
  admin/             the dashboard
    auth.py          admin password gate (candidate routes stay open)
    status.py        component health checks
    web.py           SPA serving + /system/status
    static/          index.html, app.js, styles.css (no build step)
  main.py            FastAPI
  cli.py             Typer
docs/
  GOOGLE_SETUP.md
  GOING_PUBLIC.md
scripts/
  smoke_test.py      128 offline checks — no Google, no model, no cost, nothing sent
  verify_all.py      61 end-to-end checks against a live in-process server
  demo_seed.py       a job with candidates across every funnel state
  bench_models.py    compare every installed Ollama model on screening accuracy
  bench_case.py      re-run one benchmark case while iterating on the prompt
```

---

## Notes and limits

- **Verified:** parsing, question mapping, both rule stages, strictness behaviour, score
  reconciliation, the evidence cap, and every API route — 43 offline checks in
  `scripts/smoke_test.py`, all passing. Screening quality was measured live against all five
  local models (see below).
- **Not verified live:** the Google API calls, which need OAuth credentials this machine
  does not have yet. The OpenAI backend is also unverified against a real key — its schema
  satisfies strict-mode rules (`additionalProperties: false`, every property required, no
  unsupported keywords) and the request shape is asserted against a mocked client, but no
  real round-trip has happened.
- **Local model quality is the real limit here.** A 4-8B open model is a weaker screener
  than a frontier hosted model — expect softer calibration and occasional generous scores.
  Two things contain that: the deterministic rules already removed the clearly unqualified
  before the model sees anyone, and every score carries a written rationale you can audit.
  Run `scripts/bench_models.py` after changing models, and treat the shortlist as a ranked
  queue for a human rather than an automatic accept.
- **Compensation parsing assumes Indian shorthand** (`12 LPA` → 1,200,000; `1.2 Cr`;
  `90k`). Adjust `_CTC_SUFFIXES` in `app/parsing.py` for other markets.
- **Google Forms API cannot create file-upload questions**, which is why the form asks for a
  resume *link*. If the admin adds an upload question by hand, `link-form` maps it and
  responses come through as Drive URLs.
- **Adopted forms rely on fuzzy title matching.** `link-form` prints exactly what it mapped
  and what it did not — check that output before syncing. Unmapped answers are never lost;
  they stay in `Candidate.raw_response`.
- **Refusals are not scored.** If the model declines to assess an application, the
  candidate is left unscreened and reported, rather than being silently rejected.
#   a i _ i n t e r v i e w e r _ p i p e l i n e  
 