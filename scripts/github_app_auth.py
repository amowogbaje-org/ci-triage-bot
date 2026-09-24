"""
GitHub App authentication.

Flow:
  1. Build a short-lived JWT signed with the App's private key (RS256).
  2. Use that JWT to look up the installation ID for this repo
     (unless GH_APP_INSTALLATION_ID is already set as a secret).
  3. Exchange the JWT for a scoped installation access token, which is
     what we actually use to call the REST API (logs, comments, etc).

This avoids ever using a long-lived personal access token.
"""

from __future__ import annotations

import os
import time

import jwt  # PyJWT
import requests

GITHUB_API = "https://api.github.com"


class GitHubAppAuthError(RuntimeError):
    pass


def _build_app_jwt(app_id: str, private_key_pem: str) -> str:
    now = int(time.time())
    payload = {
        "iat": now - 60,          # allow for clock drift
        "exp": now + (9 * 60),    # GitHub caps this at 10 minutes
        "iss": app_id,
    }
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


def _discover_installation_id(app_jwt: str, owner: str, repo: str) -> str:
    """Ask GitHub which installation of this App is attached to owner/repo."""
    resp = requests.get(
        f"{GITHUB_API}/repos/{owner}/{repo}/installation",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise GitHubAppAuthError(
            "Could not find an installation of this GitHub App on "
            f"{owner}/{repo} (status {resp.status_code}): {resp.text}\n"
            "Make sure the App is installed on this repository."
        )
    return str(resp.json()["id"])


def get_installation_token(owner: str, repo: str) -> str:
    """
    Returns a short-lived (1 hour) installation access token scoped to
    whatever permissions the GitHub App was granted.
    """
    app_id = os.environ.get("GH_APP_ID")
    private_key_pem = os.environ.get("GH_APP_PRIVATE_KEY")

    if not app_id or not private_key_pem:
        raise GitHubAppAuthError(
            "GH_APP_ID and GH_APP_PRIVATE_KEY must be set as secrets."
        )

    # Private keys often get mangled (literal \n) when passed through
    # GitHub Actions secrets -> env vars. Normalize just in case.
    private_key_pem = private_key_pem.replace("\\n", "\n")

    app_jwt = _build_app_jwt(app_id, private_key_pem)

    installation_id = os.environ.get("GH_APP_INSTALLATION_ID") or _discover_installation_id(
        app_jwt, owner, repo
    )

    resp = requests.post(
        f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
    if resp.status_code != 201:
        raise GitHubAppAuthError(
            f"Failed to mint installation token (status {resp.status_code}): {resp.text}"
        )
    return resp.json()["token"]
