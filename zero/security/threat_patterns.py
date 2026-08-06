"""Zero v2 threat patterns — ported from Hermes (``tools/threat_patterns.py``).

Single source of truth for prompt-injection / promptware / exfiltration patterns.
Organized by ATTACK CLASS, not source file. Each tuple is
``(regex, pattern_id, scope)`` where ``scope`` selects which scanner uses it:

    - ``all``     — applied everywhere (classic prompt injection, exfiltration)
    - ``context`` — applied to context files + memory + tool results
    - ``strict``  — applied to memory writes + skill installs only

The ``MAX_SCAN_CHARS`` cap prevents pathological scans on huge inputs.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

__all__ = [
    "MAX_SCAN_CHARS",
    "PATTERNS",
    "ThreatFinding",
    "ThreatScope",
    "ThreatSeverity",
    "scan_content",
    "scan_strict",
    "scan_text",
]


class ThreatSeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


ThreatScope = Literal["all", "context", "strict"]


@dataclass(frozen=True, slots=True)
class ThreatFinding:
    pattern_id: str
    severity: ThreatSeverity
    scope: ThreatScope
    match: str
    start: int
    end: int
    description: str

    def to_log_dict(self) -> Mapping[str, str | int]:
        return {
            "pattern_id": self.pattern_id,
            "severity": self.severity.value,
            "scope": self.scope,
            "match_snippet": self.match[:80],
            "start": self.start,
            "end": self.end,
        }


MAX_SCAN_CHARS: Final[int] = 65_536


@dataclass(frozen=True, slots=True)
class _Pattern:
    regex: re.Pattern[str]
    pattern_id: str
    severity: ThreatSeverity
    scope: ThreatScope
    description: str


# Filler between key tokens to defeat multi-word bypass.
# Bounded to 8 words to avoid catastrophic backtracking.
_FILLER = r"(?:\w+\s+){0,8}"


def _p(
    pattern: str,
    pattern_id: str,
    severity: ThreatSeverity,
    scope: ThreatScope,
    description: str,
) -> _Pattern:
    return _Pattern(
        regex=re.compile(pattern, re.IGNORECASE | re.MULTILINE),
        pattern_id=pattern_id,
        severity=severity,
        scope=scope,
        description=description,
    )


# ---------------------------------------------------------------------- pattern database
#
# Patterns are anchored on attack-specific vocabulary (C2 frameworks, exfil
# commands, promptware markers), NOT bossy English. Common phrases like
# "ignore previous instructions" are too noisy — we focus on high-signal
# attack shapes.

PATTERNS: Final[tuple[_Pattern, ...]] = (
    # -- Classic prompt injection (scope="all") --
    _p(
        r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions",
        "PI-001", ThreatSeverity.HIGH, "all",
        "Classic prompt injection: 'ignore previous instructions'",
    ),
    _p(
        r"disregard\s+(?:all\s+)?(?:previous|prior)\s+(?:instructions?|prompts?|messages?)",
        "PI-002", ThreatSeverity.HIGH, "all",
        "Classic prompt injection: 'disregard previous instructions'",
    ),
    _p(
        r"you\s+are\s+(?:now\s+)?(?:in\s+)?(?:developer|debug|root|admin|sudo|jailbreak)\s+mode",
        "PI-003", ThreatSeverity.HIGH, "all",
        "Mode-elevation prompt injection",
    ),
    _p(
        r"system\s*:\s*you\s+are\s+(?:now|actually)",
        "PI-004", ThreatSeverity.HIGH, "all",
        "Fake system message injection",
    ),

    # -- Identity hijack / role-play (scope="context") --
    _p(
        r"from\s+now\s+on[,\s]+you\s+are\s+(?:not\s+)?(?:an?\s+)?[\w\s]{3,30}(?:assistant|ai|agent|bot)",
        "IH-001", ThreatSeverity.MEDIUM, "context",
        "Identity hijack: 'from now on you are ...'",
    ),
    _p(
        r"pretend\s+(?:that\s+)?you\s+are\s+(?:a|an)\s+(?:different|unrestricted|unfiltered)",
        "IH-002", ThreatSeverity.MEDIUM, "context",
        "Identity hijack: 'pretend you are unrestricted'",
    ),

    # -- C2 / promptware markers (scope="context") --
    # Brainworm / known promptware tokens
    _p(
        r"\b(?:brainworm|c2_command|exfil_payload|persist_payload)\b",
        "C2-001", ThreatSeverity.CRITICAL, "context",
        "Known promptware marker",
    ),
    # Known C2 frameworks
    _p(
        r"\b(?:cobalt\s*strike|sliver|havoc|mythic|metasploit)\b",
        "C2-002", ThreatSeverity.HIGH, "context",
        "Known C2 framework name",
    ),
    # Promptware beacon pattern (URL with secret extraction)
    _p(
        rf"(?:exfil|post|send|upload)\s+(?:to|at|via)\s+https?://[^\s]{{10,}}{ _FILLER }(?:token|key|secret|password|env)",
        "C2-003", ThreatSeverity.CRITICAL, "context",
        "Promptware exfiltration beacon",
    ),

    # -- Exfiltration (scope="all") --
    _p(
        rf"curl\s+(?:-[A-Za-z]+\s+)*['\"]?https?://[^\s]{{10,}}{ _FILLER }(?:<|<\s*\.env|<\s*\.ssh|<\s*\.aws)",
        "EX-001", ThreatSeverity.CRITICAL, "all",
        "Shell exfiltration via curl with secret file redirect",
    ),
    _p(
        r"wget\s+(?:--post-data|--post-file)\s+[\"']?[\w./~-]+",
        "EX-002", ThreatSeverity.HIGH, "all",
        "Shell exfiltration via wget POST",
    ),
    _p(
        r"\b(?:cat|head|tail|less|more)\s+(?:/etc/(?:shadow|passwd|sudoers)|~/\.ssh/id_|/proc/self/environ)",
        "EX-003", ThreatSeverity.HIGH, "all",
        "Direct read of sensitive system file",
    ),

    # -- Persistence (scope="context") --
    _p(
        r"(?:echo|cat|tee)\s+['\"]?[^|>]+['\"]?\s*>>?\s*(?:~/\.bashrc|~/\.bash_profile|~/\.profile|~/\.zshrc|/etc/cron\.\w+/[\w.-]+)",
        "PS-001", ThreatSeverity.HIGH, "context",
        "Persistence via shell RC / cron injection",
    ),
    _p(
        r"\b(?:crontab|at|systemctl\s+enable)\s+",
        "PS-002", ThreatSeverity.MEDIUM, "context",
        "Scheduler-based persistence",
    ),

    # -- Anti-forensic (scope="context") --
    _p(
        r"\b(?:rm\s+-rf?|shred|sdelete)\s+(?:/var/log|~/\.bash_history|/etc/audit|/var/lib/audit)",
        "AF-001", ThreatSeverity.HIGH, "context",
        "Anti-forensic log deletion",
    ),
    _p(
        r"\b(?:history\s+-c|unset\s+HISTFILE|export\s+HISTFILE=/dev/null)",
        "AF-002", ThreatSeverity.MEDIUM, "context",
        "Anti-forensic history clear",
    ),

    # -- Env-var unsetting (scope="context") — targets agent runtimes --
    _p(
        r"unset\s+(?:ZERO_|HERMES_|OPENAI_|ANTHROPIC_|ROUTER_)[A-Z_]+",
        "AF-003", ThreatSeverity.HIGH, "context",
        "Unsetting agent runtime env vars",
    ),

    # -- Skill / memory write threats (scope="strict") --
    _p(
        r"<system_prompt>\s*[\s\S]{0,200}?</system_prompt>",
        "SK-001", ThreatSeverity.HIGH, "strict",
        "Embedded fake <system_prompt> tag in skill content",
    ),
    _p(
        r"<!--\s*system[:\s].*?(?:ignore|disregard|override).*?-->",
        "SK-002", ThreatSeverity.HIGH, "strict",
        "HTML comment-based system override attempt",
    ),
    _p(
        r"(?:^|\n)\s*```(?:system|developer)\s*\n[\s\S]{10,}?```",
        "SK-003", ThreatSeverity.HIGH, "strict",
        "Fenced 'system' or 'developer' role block in content",
    ),
)


# ---------------------------------------------------------------------- scanner

def scan_text(text: str, *, scope: ThreatScope = "all") -> list[ThreatFinding]:
    """Scan ``text`` for threats at or below ``scope``.

    Scope hierarchy: ``all`` < ``context`` < ``strict``.
    Scanning at ``strict`` includes ``context`` and ``all`` patterns.
    """
    if not text:
        return []

    # Hard cap on input size — protects against pathological scans.
    scan_text_slice = text[:MAX_SCAN_CHARS]

    scope_order: dict[ThreatScope, int] = {"all": 0, "context": 1, "strict": 2}
    requested_level = scope_order[scope]

    findings: list[ThreatFinding] = []
    for pat in PATTERNS:
        if scope_order[pat.scope] > requested_level:
            continue
        for m in pat.regex.finditer(scan_text_slice):
            findings.append(
                ThreatFinding(
                    pattern_id=pat.pattern_id,
                    severity=pat.severity,
                    scope=pat.scope,
                    match=m.group(0),
                    start=m.start(),
                    end=m.end(),
                    description=pat.description,
                )
            )
    return findings


def scan_content(text: str) -> list[ThreatFinding]:
    """Scan at ``context`` level — for context files, tool results, memory writes."""
    return scan_text(text, scope="context")


def scan_strict(text: str) -> list[ThreatFinding]:
    """Scan at ``strict`` level — for memory writes, skill installs."""
    return scan_text(text, scope="strict")
