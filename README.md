# CI Failure Triage Bot

Automatically diagnoses failed GitHub Actions runs and posts an AI-generated
root-cause summary as a PR (or commit) comment. No server, no hosting —
it runs entirely inside GitHub Actions, and other repos can use it as a
**reusable workflow** without copying any code.

```
workflow completes (in this repo, or any repo that calls in)
      │
      ▼
workflow_run "completed" event fires
      │
      ▼
that repo's own triage.yml calls .github/workflows/triage-reusable.yml@main
from THIS repo, which checks out scripts/ and runs triage.py:
  - conclusion == "success": posts a short "✅ passed" comment
  - conclusion == "failure":
      1. auth as GitHub App → short-lived installation token
      2. GET failed jobs for the run
      3. download logs.zip, trim to the lines around the error
      4. send excerpt to Groq (gpt-oss-120b) → { likely_cause, affected_step, confidence, suggested_fix }
      5. POST/PATCH the diagnosis as a comment
  - anything else (cancelled, skipped, timed_out, ...): no comment
Retriggers PATCH the same comment in place rather than adding a new one,
so the thread always shows just the latest result.
```

## Two workflow files, two different jobs

- **`triage-reusable.yml`** — the actual product. Declares `on: workflow_call`
  and does all the real work (auth, log parsing, LLM call, commenting).
  Lives only here; no other repo needs its own copy.
- **`triage-demo-caller.yml`** — a thin example showing how a consuming repo
  uses it: listens for `workflow_run` on a specific workflow name, then
  `uses:` the reusable workflow above. In this repo it points at
  `demo-broken-ci.yml`, purely to prove the whole thing works end to end.

Any other repo that wants triage only ever needs a file shaped like
`triage-demo-caller.yml` — see below.


## Why a GitHub App instead of a PAT

A GitHub App token is short-lived (1 hour), scoped only to the
permissions you explicitly grant, and scoped only to the repos it's
installed on. A personal access token is long-lived and tied to a human
account. For a bot that reads Actions logs and writes comments, the App
is the least-privilege choice.

## Setup

### 1. Create the GitHub App

1. GitHub → Settings → Developer settings → GitHub Apps → New GitHub App
2. Permissions:
   - **Actions**: Read-only (to fetch run logs)
   - **Contents**: Read-only (checkout, if you extend the bot)
   - **Pull requests**: Read & write (to post PR comments)
   - **Commits**: Read & write *(needed only if you want commit comments on
     pushes with no associated PR — covered by "Contents" write in newer
     API versions; check current GitHub docs if this changes)*
3. Subscribe to no webhook events — we drive this off `workflow_run`, not
   an external webhook.
4. Generate a private key (downloads a `.pem` file). Keep it safe.
5. Install the App on the repo(s) you want triaged.
6. Note the **App ID** (shown on the App's settings page).

### 2. Add repo secrets

Repo → Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `GH_APP_ID` | the App ID from step 1 |
| `GH_APP_PRIVATE_KEY` | full contents of the `.pem` file |
| `GH_APP_INSTALLATION_ID` | optional — only set this if auto-discovery fails |
| `GROQ_API_KEY` | your Groq API key (free tier, no card needed — see below) |

#### Getting a Groq API key

1. Go to **https://console.groq.com** and sign in (GitHub/Google login works, no card required).
2. **API Keys** in the left sidebar → **Create API Key**.
3. Copy the value (starts with `gsk_...`) — shown once — and save it as the `GROQ_API_KEY` secret above.

The bot uses `openai/gpt-oss-120b`, Groq's recommended replacement for the now-decommissioned Llama 3.3 70B model (as of their 2026-08-16 deprecation — check [console.groq.com/docs/deprecations](https://console.groq.com/docs/deprecations) if this ever breaks again). Groq's free tier (~14,400 requests/day, no billing) is far more than this bot will ever need, since it only calls the API once per failed job.

### 3. Point the demo caller at your real workflow (this repo only)

Edit `.github/workflows/triage-demo-caller.yml`:

```yaml
on:
  workflow_run:
    workflows: ["Your Actual CI Workflow Name"]
    types: [completed]
```

GitHub's `workflow_run` trigger requires the exact `name:` of the
workflow(s) you want to listen to — wildcards aren't supported. (If you're
setting this up in a *different* repo rather than this one, see "Using this
in another repo" below instead — you don't edit anything in this repo.)

### 4. Push to `main`

`workflow_run` only fires for workflows defined on the default branch, so
`triage-demo-caller.yml` and the workflow it's watching both need to exist
on `main` before the trigger will work.

## Using this in another repo

Once this repo (`ci-triage-bot`) is set up and working, any other repo you
own can get triage without copying `scripts/` or any Python — they call the
reusable workflow (`triage-reusable.yml`) that lives only here.

1. In the other repo, add `.github/workflows/triage.yml`:

```yaml
on:
  workflow_run:
    workflows: ["That Repo's Real CI Workflow Name"]
    types: [completed]

jobs:
  triage:
    uses: amowogbaje/ci-triage-bot/.github/workflows/triage-reusable.yml@main
    secrets:
      GH_APP_ID: ${{ secrets.GH_APP_ID }}
      GH_APP_PRIVATE_KEY: ${{ secrets.GH_APP_PRIVATE_KEY }}
      GROQ_API_KEY: ${{ secrets.GROQ_API_KEY }}
```

2. Add the same secrets (`GH_APP_ID`, `GH_APP_PRIVATE_KEY`, `GROQ_API_KEY`)
   to that repo — the reusable workflow only receives secrets explicitly
   passed to it here, it does not pull them from `ci-triage-bot`.
3. Install the same GitHub App on that repo too (Settings → GitHub Apps →
   your app → Configure → add the repo, or install fresh if it's a
   different account/org).
4. **If `ci-triage-bot` is private**, you also need to explicitly allow
   cross-repo access once: in *this* repo → Settings → Actions → General →
   scroll to **Access** → select *"Accessible from repositories owned by
   '<your username>' user"* → Save. If `ci-triage-bot` is public (or once
   you move things into an organization and both repos are in it), this
   step isn't needed.

That's it — no `scripts/` folder, no `requirements.txt`, nothing Python in
the consuming repo at all.

## Proof of working (this repo)

`.github/workflows/demo-broken-ci.yml` runs `tests/test_broken_on_purpose.py`,
which contains two deliberately failing tests (a `ZeroDivisionError` and a
plain failed assertion). Every push/PR triggers it, it fails, and
`triage-demo-caller.yml` fires in response — visible in this repo's **Actions** tab and
as real comments in PR history.

To trigger it yourself:

```bash
git checkout -b demo-failure
git commit --allow-empty -m "trigger demo CI failure"
git push origin demo-failure
# open a PR — watch the Actions tab, then check the PR for the triage comment
```

## Local testing

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in real values
# Save a real workflow_run payload (from a past failed run's API response,
# or GitHub's docs) to sample_event.json, then:
cd scripts
python -c "from dotenv import load_dotenv; load_dotenv('../.env')" && python triage.py
```

## Retries update the same comment

Each comment embeds a hidden marker (`<!-- ci-triage-bot:<workflow name> -->`).
On a new run, the bot lists existing comments on the PR (or commit), and if
it finds its own marker, `PATCH`es that comment instead of posting a new
one — so re-running a flaky job doesn't spam the thread with duplicate
diagnoses. The marker is scoped per workflow name, so if you triage
multiple workflows on the same PR they get separate, independently-updated
comments.

## Cost & log size notes

- Log excerpts are trimmed to ~40 lines around the first `##[error]`
  marker (falling back to keyword search, then the log tail) and capped
  at 6,000 characters before being sent to the model — keeps each
  diagnosis to a single small, cheap API call regardless of how noisy the
  raw log is.
- One LLM call per **failed job**, not per run, so a run with 3 failed
  matrix jobs produces one comment with 3 sections, at 3 calls' cost.

## Files

```
.github/workflows/
  triage-reusable.yml    # the actual product — on: workflow_call
  triage-demo-caller.yml # example consumer: triggers on demo-broken-ci.yml
  demo-broken-ci.yml     # intentionally-broken workflow, for proof of working
scripts/
  triage.py              # entry point / orchestration
  github_app_auth.py     # JWT + installation token exchange
  log_parser.py          # log download + excerpt extraction
tests/
  test_broken_on_purpose.py
requirements.txt
.env.example
```
