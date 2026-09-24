"""
Downloads the logs for a failed workflow run and trims them down to the
part actually worth sending to the LLM: the failed job, and within that
job, the lines around the first GitHub Actions "##[error]" marker (or
the tail of the log if no marker is found).
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from typing import Optional

import requests

GITHUB_API = "https://api.github.com"
MAX_EXCERPT_CHARS = 6000  # keep the LLM prompt small and cheap
CONTEXT_LINES_AFTER_ERROR = 40
CONTEXT_LINES_BEFORE_ERROR = 15


@dataclass
class FailedJob:
    id: int
    name: str
    failed_step: Optional[str]
    html_url: str


def get_failed_jobs(token: str, owner: str, repo: str, run_id: int) -> list[FailedJob]:
    resp = requests.get(
        f"{GITHUB_API}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
        headers=_headers(token),
        timeout=30,
    )
    resp.raise_for_status()
    jobs = []
    for job in resp.json().get("jobs", []):
        if job.get("conclusion") != "failure":
            continue
        failed_step = next(
            (s["name"] for s in job.get("steps", []) if s.get("conclusion") == "failure"),
            None,
        )
        jobs.append(
            FailedJob(
                id=job["id"],
                name=job["name"],
                failed_step=failed_step,
                html_url=job["html_url"],
            )
        )
    return jobs


def download_run_logs_zip(token: str, owner: str, repo: str, run_id: int) -> zipfile.ZipFile:
    resp = requests.get(
        f"{GITHUB_API}/repos/{owner}/{repo}/actions/runs/{run_id}/logs",
        headers=_headers(token),
        timeout=60,
    )
    resp.raise_for_status()
    return zipfile.ZipFile(io.BytesIO(resp.content))


def extract_excerpt_for_job(logs_zip: zipfile.ZipFile, job_name: str) -> str:
    """
    The logs zip has one folder per job (named after the job), containing
    one numbered .txt file per step. We grab everything for that job,
    concatenate it in step order, then trim around the error.
    """
    candidates = [
        n
        for n in logs_zip.namelist()
        if n.startswith(f"{job_name}/") and n.endswith(".txt")
    ]
    candidates.sort()  # step files are zero-padded, so lexicographic == chronological

    if not candidates:
        # Fall back to a fuzzy match in case of slug differences
        candidates = sorted(
            n for n in logs_zip.namelist()
            if job_name.split(" ")[0].lower() in n.lower() and n.endswith(".txt")
        )

    full_text = ""
    for name in candidates:
        with logs_zip.open(name) as f:
            full_text += f.read().decode("utf-8", errors="replace")
            full_text += "\n"

    return _trim_around_error(full_text)


def _trim_around_error(full_text: str) -> str:
    lines = full_text.splitlines()

    error_idx = next(
        (i for i, line in enumerate(lines) if "##[error]" in line),
        None,
    )

    if error_idx is None:
        # No explicit marker; look for common failure keywords instead.
        pattern = re.compile(r"(Traceback|Error:|FAILED|Exception|panicked at|npm ERR!)")
        error_idx = next(
            (i for i, line in enumerate(lines) if pattern.search(line)),
            None,
        )

    if error_idx is not None:
        start = max(0, error_idx - CONTEXT_LINES_BEFORE_ERROR)
        end = min(len(lines), error_idx + CONTEXT_LINES_AFTER_ERROR)
        excerpt = "\n".join(lines[start:end])
    else:
        # No obvious marker at all; just take the tail of the log.
        excerpt = "\n".join(lines[-80:])

    excerpt = _strip_timestamps(excerpt)
    return excerpt[:MAX_EXCERPT_CHARS]


def _strip_timestamps(text: str) -> str:
    # GitHub Actions prefixes every line with an ISO-8601 timestamp; strip
    # it so it doesn't eat into the LLM's context budget for no reason.
    return re.sub(r"^\d{4}-\d{2}-\d{2}T[\d:.Z]+ ", "", text, flags=re.MULTILINE)


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
