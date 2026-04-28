from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests


@dataclass
class GitHubConfig:
    token: str
    repo: str  # owner/name


def get_github_config() -> GitHubConfig | None:
    token = (os.getenv("GITHUB_TOKEN", "") or "").strip()
    repo = (os.getenv("GITHUB_REPO", "") or "").strip()
    if not token or not repo or "/" not in repo:
        return None
    return GitHubConfig(token=token, repo=repo)


def create_issue(*, title: str, body_md: str, labels: Optional[List[str]] = None) -> str:
    """
    Create an issue in GITHUB_REPO using GITHUB_TOKEN.
    Returns the html_url of the created issue.
    """
    cfg = get_github_config()
    if not cfg:
        raise RuntimeError("GITHUB_TOKEN/GITHUB_REPO não configurados.")

    url = f"https://api.github.com/repos/{cfg.repo}/issues"
    headers = {
        "Authorization": f"Bearer {cfg.token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload: Dict[str, Any] = {"title": title, "body": body_md}
    if labels:
        payload["labels"] = labels
    r = requests.post(url, json=payload, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()
    return str(data.get("html_url") or "")

