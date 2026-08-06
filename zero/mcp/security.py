"""MCP security scanner — ported from Hermes ``hermes_cli/mcp_security.py``.

Blocks two high-signal abuse shapes at save AND spawn time:

    1. Exfiltration: shell interpreter whose inline script invokes network
       egress tooling (curl, wget, nc, /dev/tcp, Invoke-WebRequest, etc.)
    2. Persistence: shell interpreter whose inline script writes to OS
       persistence surfaces (~/.ssh/authorized_keys, /etc/pam.d, cron, etc.)

Per ADR T-8.4: dual save+spawn check — a hand-edited config must not bypass
the gate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "McpSecurityError",
    "McpSecurityFinding",
    "scan_mcp_command_for_exfiltration",
    "scan_mcp_command_for_persistence",
    "scan_mcp_server_config",
    "EXFIL_PATTERNS",
    "PERSISTENCE_PATTERNS",
    "INTERPRETER_NAMES",
]


class McpSecurityError(RuntimeError):
    """Raised when an MCP server config contains a forbidden pattern."""


@dataclass(frozen=True, slots=True)
class McpSecurityFinding:
    """A single security finding from MCP config scan."""

    kind: str  # "exfiltration" or "persistence"
    pattern_id: str
    match: str
    description: str


# Shell interpreters whose inline scripts we scan.
INTERPRETER_NAMES = frozenset({
    "bash", "sh", "zsh", "fish", "dash", "ksh",
    "python", "python3", "python2",
    "node", "ruby", "perl", "php",
    "powershell", "pwsh",
    "cmd",
})

# Patterns that indicate exfiltration (network egress from a shell command).
EXFIL_PATTERNS: dict[str, re.Pattern[str]] = {
    "curl": re.compile(r"\bcurl\b", re.IGNORECASE),
    "wget": re.compile(r"\bwget\b", re.IGNORECASE),
    "nc": re.compile(r"\b(?:nc|ncat|netcat)\b", re.IGNORECASE),
    "socat": re.compile(r"\bsocat\b", re.IGNORECASE),
    "dev_tcp": re.compile(r"/dev/tcp/", re.IGNORECASE),
    "powershell_iwr": re.compile(r"Invoke-(?:WebRequest|RestMethod)", re.IGNORECASE),
    "powershell_webclient": re.compile(r"System\.Net\.WebClient", re.IGNORECASE),
    "python_requests": re.compile(r"\brequests\.(?:get|post|put|delete|patch)\s*\(", re.IGNORECASE),
    "python_urllib": re.compile(r"urllib\.request\.urlopen", re.IGNORECASE),
}

# Patterns that indicate persistence (writing to OS persistence surfaces).
PERSISTENCE_PATTERNS: dict[str, re.Pattern[str]] = {
    "authorized_keys": re.compile(r"authorized_keys", re.IGNORECASE),
    "ssh_dir": re.compile(r"~/\.ssh/|/etc/ssh/", re.IGNORECASE),
    "pam": re.compile(r"/etc/pam\.d/|pam_[a-z_]+\.so", re.IGNORECASE),
    "sudoers": re.compile(r"/etc/sudoers", re.IGNORECASE),
    "cron": re.compile(r"/etc/cron[\./]|crontab\s+-", re.IGNORECASE),
    "rc_local": re.compile(r"/etc/rc\.local", re.IGNORECASE),
    "systemd": re.compile(r"/etc/systemd/", re.IGNORECASE),
    "bashrc": re.compile(r"~/\.bashrc|~/\.bash_profile|~/\.profile|~/\.zshrc", re.IGNORECASE),
}


def scan_mcp_command_for_exfiltration(command: str, args: list[str]) -> McpSecurityFinding | None:
    """Detect exfiltration patterns in an MCP server's command + args.

    Returns the first finding, or None if clean.
    """
    # Combine command + args for scanning.
    full = " ".join([command, *args])
    # Check if any interpreter is involved.
    cmd_lower = command.lower()
    is_interpreter = any(interp in cmd_lower for interp in INTERPRETER_NAMES)
    if not is_interpreter:
        return None
    # Scan combined string for exfil patterns.
    for name, pattern in EXFIL_PATTERNS.items():
        m = pattern.search(full)
        if m:
            return McpSecurityFinding(
                kind="exfiltration",
                pattern_id=f"EXFIL-{name}",
                match=m.group(0),
                description=f"MCP command invokes network egress tooling: {m.group(0)!r}",
            )
    return None


def scan_mcp_command_for_persistence(command: str, args: list[str]) -> McpSecurityFinding | None:
    """Detect persistence patterns in an MCP server's command + args."""
    full = " ".join([command, *args])
    cmd_lower = command.lower()
    is_interpreter = any(interp in cmd_lower for interp in INTERPRETER_NAMES)
    if not is_interpreter:
        return None
    for name, pattern in PERSISTENCE_PATTERNS.items():
        m = pattern.search(full)
        if m:
            return McpSecurityFinding(
                kind="persistence",
                pattern_id=f"PERSIST-{name}",
                match=m.group(0),
                description=f"MCP command writes to OS persistence surface: {m.group(0)!r}",
            )
    return None


def scan_mcp_server_config(
    *,
    command: str,
    args: list[str],
    env: dict[str, str] | None = None,
) -> list[McpSecurityFinding]:
    """Run all MCP security scans on a server config.

    Returns list of findings (empty if clean).
    """
    findings: list[McpSecurityFinding] = []
    exfil = scan_mcp_command_for_exfiltration(command, args)
    if exfil:
        findings.append(exfil)
    persist = scan_mcp_command_for_persistence(command, args)
    if persist:
        findings.append(persist)

    # Also scan env vars for sensitive values that shouldn't be inline.
    if env:
        for key, value in env.items():
            key_lower = key.lower()
            if any(s in key_lower for s in ("token", "secret", "key", "password", "passwd")):
                # Allow short env var references; flag if the value looks like a real secret.
                if len(value) > 20 and not value.startswith("$"):
                    findings.append(McpSecurityFinding(
                        kind="exfiltration",
                        pattern_id="ENV-INLINE-SECRET",
                        match=f"{key}=***",
                        description=(
                            f"MCP server env var {key!r} contains an inline secret value — "
                            "use a secret:// reference instead"
                        ),
                    ))
    return findings
