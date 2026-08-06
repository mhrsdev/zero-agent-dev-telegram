"""Zero v2 GitHub integration — Phase 5.

Per ADR 0004: GitHub owns code, branches, PRs, CODEOWNERS.
Zero is a pure consumer via GitHub API.

Token via ``secret://``, never stored as raw value.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

from zero.core.secret import SecretResolver, SecretValue

__all__ = [
    "GitHubClient",
    "GitHubError",
    "GitHubPR",
    "GitHubRepo",
]


class GitHubError(RuntimeError):
    """Raised on GitHub API errors."""


@dataclass(frozen=True, slots=True)
class GitHubRepo:
    full_name: str  # "owner/repo"
    default_branch: str
    private: bool


@dataclass(frozen=True, slots=True)
class GitHubPR:
    number: int
    title: str
    state: Literal["open", "closed"]
    draft: bool
    head_ref: str
    base_ref: str
    mergeable: bool | None = None


@dataclass
class GitHubClient:
    """Async GitHub REST API client.

    Token is resolved at call time via ``secret://`` reference.
    """

    token_ref: str  # e.g. "secret://env/GITHUB_TOKEN"
    resolver: SecretResolver
    api_base: str = "https://api.github.com"
    _token: str | None = field(default=None, init=False, repr=False)

    async def _get_token(self) -> str:
        if self._token is None:
            secret: SecretValue = self.resolver.resolve(self.token_ref)
            self._token = secret.reveal()
        return self._token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        import httpx  # noqa: PLC0415

        token = await self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"{self.api_base}{path}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(method, url, json=json_body, params=params, headers=headers)
            if resp.status_code >= 400:
                raise GitHubError(
                    f"GitHub API {method} {path} returned {resp.status_code}: {resp.text[:200]}"
                )
            return resp.json() if resp.text else {}

    async def get_repo(self, full_name: str) -> GitHubRepo:
        data: dict[str, Any] = await self._request("GET", f"/repos/{full_name}")
        return GitHubRepo(
            full_name=data["full_name"],
            default_branch=data["default_branch"],
            private=data["private"],
        )

    async def list_prs(self, full_name: str, *, state: str = "open") -> list[GitHubPR]:
        data: list[dict[str, Any]] = await self._request(
            "GET",
            f"/repos/{full_name}/pulls",
            params={"state": state},
        )
        return [
            GitHubPR(
                number=pr["number"],
                title=pr["title"],
                state=pr["state"],
                draft=pr["draft"],
                head_ref=pr["head"]["ref"],
                base_ref=pr["base"]["ref"],
                mergeable=pr.get("mergeable"),
            )
            for pr in data
        ]

    async def create_pr(
        self,
        full_name: str,
        *,
        title: str,
        head: str,
        base: str,
        body: str = "",
        draft: bool = True,  # ADR T-5.5: always draft
    ) -> GitHubPR:
        data: dict[str, Any] = await self._request(
            "POST",
            f"/repos/{full_name}/pulls",
            json_body={
                "title": title,
                "head": head,
                "base": base,
                "body": body,
                "draft": draft,
            },
        )
        return GitHubPR(
            number=data["number"],
            title=data["title"],
            state=data["state"],
            draft=data["draft"],
            head_ref=data["head"]["ref"],
            base_ref=data["base"]["ref"],
        )
