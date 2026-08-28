"""S0 — seed the real runtime world (idempotent).

Creates: demo git repository, user plugin (skills analogue), verified
telegram identity, live binding to the real group (with the wizard's bot
token secret ref), one registered repository, agent types (team roles)
with knowledge, tool capability grants for the worktree toolset + plugin.
"""

from __future__ import annotations

import subprocess
import sys

sys.path.insert(0, "/home/z/my-project/scripts/realrun")
from env_common import (  # noqa: E402
    GROUP_ID,
    REAL_HOME,
    REPO,
    TG_SENDER_ID,
    build_real_services,
    management_project,
    read_state,
    record,
    setup_env,
)

setup_env()


def ensure_demo_repo() -> None:
    if (REPO / ".git").exists():
        print(f"repo exists: {REPO}")
        return
    REPO.mkdir(parents=True, exist_ok=True)
    (REPO / "README.md").write_text(
        "# textkit workspace\n\nBase repository for the Zero real-run coding tasks.\n",
        encoding="utf-8",
    )
    (REPO / "NOTES.md").write_text(
        "## Conventions\n\n- Pure stdlib only (no third-party deps).\n"
        "- Every module needs unittest coverage in tests/.\n"
        "- Run tests via `python3 -m unittest discover -s tests -v`.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=REPO, check=True)
    subprocess.run(["git", "config", "user.email", "zero@realrun"], cwd=REPO, check=True)
    subprocess.run(["git", "config", "user.name", "Zero RealRun"], cwd=REPO, check=True)
    subprocess.run(["git", "add", "."], cwd=REPO, check=True)
    subprocess.run(["git", "commit", "-qm", "init: textkit workspace base"], cwd=REPO, check=True)
    print(f"repo created: {REPO}")


def ensure_plugin() -> None:
    plugin = REAL_HOME / "plugins" / "wordcount.py"
    plugin.write_text(
        '"""Zero real-run plugin: wordcount tool (skills analogue)."""\n'
        "\n"
        "\n"
        "def register(manage_context):\n"
        '    """Register the wordcount extension tool."""\n'
        "\n"
        "    def wordcount_handler(input_data, context):\n"
        '        text = str(input_data.get("text", ""))\n'
        '        words = text.split()\n'
        "        return {\n"
        '            "words": len(words),\n'
        '            "chars": len(text),\n'
        '            "longest": max((len(w) for w in words), default=0),\n'
        "        }\n"
        "\n"
        "    manage_context.tool_registry.register_tool(\n"
        '        name="wordcount",\n'
        '        description="Count words, characters and the longest word length of a text.",\n'
        "        input_schema={\n"
        '            "type": "object",\n'
        '            "properties": {"text": {"type": "string"}},\n'
        '            "required": ["text"],\n'
        "        },\n"
        "        output_schema={\n"
        '            "type": "object",\n'
        '            "properties": {\n'
        '                "words": {"type": "integer"},\n'
        '                "chars": {"type": "integer"},\n'
        '                "longest": {"type": "integer"},\n'
        "            },\n"
        "        },\n"
        '        handler_key="plugin:wordcount",\n'
        "        handler=wordcount_handler,\n"
        "        inline=True,\n"
        "    )\n",
        encoding="utf-8",
    )
    print(f"plugin installed: {plugin}")


def main() -> int:
    settings, services = build_real_services()
    print("providers registered:", services.providers.registered_provider_names)
    print("planner wired:", services.planner is not None)
    print("decomposition enabled:", services.scheduler._decomposition_enabled)
    project = management_project(services)
    owner = services.identity.get_user(project.owner_user_id)
    print("project:", project.name, project.id.value, "| owner:", owner.display_name)

    # tools actually registered (worktree + plugins, dev mode)
    names = sorted(t.name for t in services.tools.list_tools())
    print("registered tools:", names)
    record("tools_registered", names)

    ensure_demo_repo()
    ensure_plugin()

    # verified telegram identity for the owner (gateway intake requires it)
    try:
        linked = services.identity.link_external_identity(
            user_id=owner.id,
            platform="telegram",
            external_id=TG_SENDER_ID,
            external_username="zero_owner",
            verified=True,
        )
        print("external identity linked:", linked)
    except Exception as exc:
        print(f"external identity already linked ({type(exc).__name__}) — kept")

    # live binding to the real group with the wizard's bot token ref
    import yaml

    cfg = yaml.safe_load((REAL_HOME / "config.yaml").read_text(encoding="utf-8"))
    token_ref = cfg["telegram"]["bot_token_ref"]
    existing = read_state("binding_id")
    binding_id = None
    if existing:
        from zero.domain.interfaces import InterfaceBindingId

        try:
            b = services.interface_transports._interface_repo.get_binding_by_id(
                project.id, InterfaceBindingId(existing)
            )
            binding_id = b.id.value
        except Exception:
            binding_id = None
    if binding_id is None:
        binding = services.interfaces.create_binding(
            project_id=project.id,
            actor_id=owner.id,
            platform="telegram",
            chat_id=GROUP_ID,
            topic_id=None,
            bot_token_ref=token_ref,
            is_enabled=True,
            source="system",
        )
        binding_id = binding.id.value
    record("binding_id", binding_id)
    print("binding:", binding_id, "chat", GROUP_ID, "token_ref", token_ref)

    # exactly one repository → runtime can auto-resolve it for coding tasks
    repos = services.worktree.list_repositories(project.id, actor_id=owner.id)
    if not repos:
        repo = services.worktree.register_repository(
            project_id=project.id,
            actor_id=owner.id,
            name="textkit-repo",
            local_path=str(REPO),
        )
        print("repository registered:", repo.name, repo.local_path)
    else:
        print("repository already registered:", repos[0].name)

    # team roles (dynamic agent types) + knowledge (long-term memory)
    have_types = {t.name for t in services.agent_types.list_types(project.id)}
    if "Implementer" not in have_types:
        impl = services.agent_types.create_type(
            project_id=project.id,
            actor_id=owner.id,
            name="Implementer",
            responsibility="Writes and tests production code for tasks",
            memory_scope="Implementation decisions, file layout, test results",
            max_concurrent_instances=2,
        )
        print("agent type created:", impl.name, impl.id.value)
    if "Reviewer" not in have_types:
        rev = services.agent_types.create_type(
            project_id=project.id,
            actor_id=owner.id,
            name="Reviewer",
            responsibility="Reviews diffs and tests for quality and contract fit",
            memory_scope="Review findings and conventions",
            max_concurrent_instances=2,
        )
        services.agent_types.add_knowledge(
            project_id=project.id,
            actor_id=owner.id,
            type_id=rev.id,
            kind="constraint",
            content="Always verify tests actually ran; require unittest discovery output.",
        )
        print("agent type created:", rev.name, rev.id.value)
    types = services.agent_types.list_types(project.id)
    record("agent_types", [t.name for t in types])

    # capability grants: worktree tools + plugin tool for the worker scope
    grants = []
    for tool_name in ("read_file", "write_file", "run_command", "capture_diff", "wordcount"):
        try:
            tool = services.tools.get_tool_by_name(tool_name)
        except Exception:
            print(f"  !! tool not found: {tool_name}")
            continue
        try:
            services.tools.grant_tool(
                project_id=project.id,
                actor_id=owner.id,
                tool_id=tool.id,
                agent_scope="main_worker",
            )
            grants.append(tool_name)
        except Exception as exc:  # already granted on re-run
            print(f"  grant {tool_name}: {type(exc).__name__} (kept)")
            grants.append(tool_name)
    record("granted_tools", grants)
    print("granted tools:", grants)

    # REAL outbound hello through the full binding transport path
    from env_common import notify

    out = notify(
        services,
        project,
        "🚀 <b>REAL RUN</b> starting — engine booted, binding live. "
        f"Provider: <code>openai-compatible</code> · Model: <code>claude-opus-5</code> · "
        f"Tools: {', '.join(grants)}",
    )
    print("real outbound hello sent:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
