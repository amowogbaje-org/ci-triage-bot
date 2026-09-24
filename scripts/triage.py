"""
CI Failure Triage Bot — entry point.

Run inside GitHub Actions in response to a `workflow_run` `completed`
event with conclusion `failure`. Reads the event payload GitHub already
gives us, fetches the failed run's logs, asks an LLM to diagnose it,
and posts the diagnosis as a PR (or commit) comment.

Required env vars:
  GH_APP_ID              - GitHub App ID
  GH_APP_PRIVATE_KEY     - GitHub App private key (PEM)
  GH_APP_INSTALLATION_ID - optional; auto-discovered if omitted
  GROQ_API_KEY            - LLM API key (console.groq.com, free tier)
  GITHUB_EVENT_PATH      - provided automatically by Actions
  GITHUB_REPOSITORY      - provided automatically by Actions, "owner/repo"
"""

from __future__ import annotations

import json
import os
import sys

import requests

from github_app_auth import get_installation_token, GitHubAppAuthError
from log_parser import get_failed_jobs, download_run_logs_zip, extract_excerpt_for_job

GITHUB_API = "https://api.github.com"
# Groq's free tier: no credit card, ~14,400 req/day, OpenAI-compatible
# chat-completions format. https://console.groq.com
GROQ_API = "https://api.groq.com/openai/v1/chat/completions"
# llama-3.3-70b-versatile was decommissioned by Groq on 2026-08-16.
# gpt-oss-120b is Groq's recommended production-tier replacement (their
# other suggested option, qwen3.6-27b, is Preview-tier and not meant for
# production use). See https://console.groq.com/docs/deprecations
GROQ_MODEL = "openai/gpt-oss-120b"

# Embedded in every comment the bot posts so it can find its own previous
# comment on retriggers and update it in place, instead of piling up a new
# comment per retry. Scoped per-workflow so multiple triaged workflows on
# the same PR don't stomp on each other.
def _marker(workflow_name: str) -> str:
    return f"<!-- ci-triage-bot:{workflow_name} -->"

SYSTEM_PROMPT = """You are a senior build engineer triaging a failed CI run. \
You will be given a trimmed excerpt of build logs. Respond with ONLY a JSON \
object (no markdown fences, no prose outside the JSON) with exactly these keys:
  "likely_cause": one or two sentence plain-English diagnosis
  "affected_step": the step/command/test that failed, best guess from the excerpt
  "confidence": one of "high", "medium", "low"
  "suggested_fix": concrete, actionable next step(s) for the developer
If the excerpt is ambiguous, say so honestly in likely_cause rather than guessing wildly.
"""


def main() -> int:
    event = _load_event()
    workflow_run = event["workflow_run"]

    if workflow_run.get("conclusion") != "failure":
        print(f"Run concluded with '{workflow_run.get('conclusion')}', not 'failure'. Nothing to do.")
        return 0

    owner, repo = os.environ["GITHUB_REPOSITORY"].split("/")
    run_id = workflow_run["id"]

    try:
        token = get_installation_token(owner, repo)
    except GitHubAppAuthError as e:
        print(f"::error::Auth failed: {e}", file=sys.stderr)
        return 1

    failed_jobs = get_failed_jobs(token, owner, repo, run_id)
    if not failed_jobs:
        print("No failed jobs found on this run (maybe it was cancelled). Nothing to do.")
        return 0

    logs_zip = download_run_logs_zip(token, owner, repo, run_id)

    comment_sections = []
    for job in failed_jobs:
        excerpt = extract_excerpt_for_job(logs_zip, job.name)
        if not excerpt.strip():
            print(f"Could not extract a log excerpt for job '{job.name}'; skipping.")
            continue
        diagnosis = _diagnose(excerpt, job.name, job.failed_step)
        comment_sections.append(_format_job_section(job, diagnosis))

    if not comment_sections:
        print("Nothing diagnosable was extracted from the logs.")
        return 0

    body = _format_comment(workflow_run, comment_sections)
    _post_comment(token, owner, repo, workflow_run, body)
    print("Posted triage comment.")
    return 0


def _load_event() -> dict:
    event_path = os.environ["GITHUB_EVENT_PATH"]
    with open(event_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _diagnose(excerpt: str, job_name: str, failed_step: str | None) -> dict:
    api_key = os.environ["GROQ_API_KEY"]
    user_prompt = (
        f"Job: {job_name}\n"
        f"Step GitHub marked as failed: {failed_step or 'unknown'}\n\n"
        f"Log excerpt:\n```\n{excerpt}\n```"
    )

    resp = requests.post(
        GROQ_API,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": GROQ_MODEL,
            "max_tokens": 500,
            "temperature": 0.2,
            # Groq supports OpenAI-style JSON mode; combined with the
            # system prompt's explicit schema this keeps output parseable.
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=60,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"].strip()

    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {
            "likely_cause": "The model's response could not be parsed as JSON.",
            "affected_step": failed_step or "unknown",
            "confidence": "low",
            "suggested_fix": f"Raw model output:\n\n{text}",
        }


def _format_job_section(job, diagnosis: dict) -> str:
    return (
        f"### ❌ `{job.name}`\n"
        f"**Failed step:** {diagnosis.get('affected_step', job.failed_step or 'unknown')}\n"
        f"**Confidence:** {diagnosis.get('confidence', 'unknown')}\n\n"
        f"**Likely cause:** {diagnosis.get('likely_cause', 'n/a')}\n\n"
        f"**Suggested fix:** {diagnosis.get('suggested_fix', 'n/a')}\n\n"
        f"[View full logs for this job]({job.html_url})"
    )


def _format_comment(workflow_run: dict, sections: list[str]) -> str:
    workflow_name = workflow_run.get("name", "workflow")
    header = (
        f"{_marker(workflow_name)}\n"
        f"## 🤖 CI Triage Bot\n"
        f"Automated diagnosis of **{workflow_name}** run "
        f"[#{workflow_run.get('run_number')}]({workflow_run.get('html_url')}) "
        f"(attempt {workflow_run.get('run_attempt', 1)})\n\n"
        f"---\n"
    )
    footer = (
        "\n---\n_This summary was generated automatically and is updated in place on "
        "retries — it reflects the most recent run only. Verify before acting on it._"
    )
    return header + "\n\n---\n\n".join(sections) + footer


def _post_comment(token: str, owner: str, repo: str, workflow_run: dict, body: str) -> None:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    pull_requests = workflow_run.get("pull_requests") or []
    workflow_name = workflow_run.get("name", "workflow")
    marker = _marker(workflow_name)

    if pull_requests:
        pr_number = pull_requests[0]["number"]
        list_url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments"
        create_url = list_url
        patch_url_tpl = f"{GITHUB_API}/repos/{owner}/{repo}/issues/comments/{{comment_id}}"
    else:
        sha = workflow_run["head_sha"]
        list_url = f"{GITHUB_API}/repos/{owner}/{repo}/commits/{sha}/comments"
        create_url = list_url
        patch_url_tpl = f"{GITHUB_API}/repos/{owner}/{repo}/comments/{{comment_id}}"

    existing_id = _find_existing_comment(headers, list_url, marker)

    if existing_id is not None:
        resp = requests.patch(
            patch_url_tpl.format(comment_id=existing_id),
            headers=headers,
            json={"body": body},
            timeout=30,
        )
    else:
        resp = requests.post(create_url, headers=headers, json={"body": body}, timeout=30)

    resp.raise_for_status()


def _find_existing_comment(headers: dict, list_url: str, marker: str) -> int | None:
    """
    Paginates through existing comments looking for one this bot already
    posted for this workflow (identified by the hidden marker), so retries
    update that comment instead of creating a new one each time.
    """
    url = list_url
    while url:
        resp = requests.get(url, headers=headers, params={"per_page": 100}, timeout=30)
        resp.raise_for_status()
        for comment in resp.json():
            if marker in (comment.get("body") or ""):
                return comment["id"]
        url = resp.links.get("next", {}).get("url")
    return None


if __name__ == "__main__":
    sys.exit(main())
