# CI Failure Triage Bot

Automatically diagnoses failed GitHub Actions runs and posts an AI-generated
root-cause summary as a PR (or commit) comment. No server, no hosting —
it runs entirely inside GitHub Actions.

```
workflow fails
      │
      ▼
workflow_run "completed" event fires
      │
      ▼
triage.yml checks conclusion == failure
      │
      ▼
scripts/triage.py:
  1. auth as GitHub App → short-lived installation token
  2. GET failed jobs for the run
  3. download logs.zip, trim to the lines around the error
  4. send excerpt to Claude → { likely_cause, affected_step, confidence, suggested_fix }
  5. POST comment on the PR (or commit, if no PR)
```

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
| `ANTHROPIC_API_KEY` | your Anthropic API key |

### 3. Point the trigger at your real workflow(s)

Edit `.github/workflows/triage.yml`:

```yaml
on:
  workflow_run:
    workflows: ["Your Actual CI Workflow Name"]
    types: [completed]
```

GitHub's `workflow_run` trigger requires the exact `name:` of the
workflow(s) you want to listen to — wildcards aren't supported.

### 4. Push to `main`

`workflow_run` only fires for workflows defined on the default branch, so
both `triage.yml` and the workflow it's watching need to exist on `main`
before the trigger will work.

## Proof of working (this repo)

`.github/workflows/demo-broken-ci.yml` runs `tests/test_broken_on_purpose.py`,
which contains two deliberately failing tests (a `ZeroDivisionError` and a
plain failed assertion). Every push/PR triggers it, it fails, and
`triage.yml` fires in response — visible in this repo's **Actions** tab and
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
  triage.yml            # the bot itself
  demo-broken-ci.yml     # intentionally-broken workflow, for proof of working
scripts/
  triage.py             # entry point / orchestration
  github_app_auth.py    # JWT + installation token exchange
  log_parser.py          # log download + excerpt extraction
tests/
  test_broken_on_purpose.py
requirements.txt
.env.example
```
