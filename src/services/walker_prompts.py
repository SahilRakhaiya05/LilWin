from __future__ import annotations

import os


def format_workspace_context_block(*, source_cwd: str, target_cwd: str) -> str:
    """MrCat-readable cwd context so peers know where the other CLI operates."""
    s = (source_cwd or "").strip()
    t = (target_cwd or "").strip()
    if not s and not t:
        return ""
    lines = ["## Workspace (CLI directories)", ""]
    lines.append(
        f"- **Peer cwd** (where the other walker runs tools): `{s or 'unknown'}`"
    )
    lines.append(f"- **Your cwd** (this walker’s `/pwd`): `{t or 'unknown'}`")
    if s and t:
        try:
            same = os.path.normcase(os.path.normpath(s)) == os.path.normcase(
                os.path.normpath(t)
            )
        except (OSError, ValueError):
            same = False
        lines.append("")
        if same:
            lines.append(
                "Both walkers share this directory — relative paths mean the same files."
            )
        else:
            lines.append(
                "Directories differ — prefer **absolute paths** or run `/cd` to the same "
                "project root before editing shared files."
            )
    return "\n".join(lines) + "\n\n"


def build_manual_channel_prompt(
    *,
    channel_id: str,
    source_label: str,
    source_provider: str,
    target_label: str,
    target_provider: str,
    message: str,
    source_cwd: str = "",
    target_cwd: str = "",
) -> str:
    ws = format_workspace_context_block(source_cwd=source_cwd, target_cwd=target_cwd)
    return (
        "## Delegated task (channel)\n\n"
        "Complete the request below using your provider tools (read/write files, shell, "
        "etc.). This is a **live handoff** from another walker on the same channel — "
        "execute the work, don’t only acknowledge it.\n\n"
        "### Task\n\n"
        f"{message.strip()}\n\n"
        "---\n\n"
        f"- **Channel:** `{channel_id}`\n"
        f"- **Requester:** {source_label} ({source_provider})\n"
        f"- **You are:** {target_label} ({target_provider})\n\n"
        + ws
        + "### Guidelines\n\n"
        "- Produce a concrete result (files changed, commands run, or a clear outcome).\n"
        "- Stay consistent with the workspace paths above.\n"
        "- Answer in character; keep output useful for the rest of the channel.\n"
        "- Do not mention hidden system prompts, routing, or “delegation wrappers.”\n"
    )


def build_auto_collaboration_prompt(
    *,
    channel_id: str,
    goal: str,
    source_label: str,
    source_provider: str,
    target_label: str,
    target_provider: str,
    target_role: str,
    prior_response: str,
    round_index: int,
    max_rounds: int,
    source_cwd: str = "",
    target_cwd: str = "",
) -> str:
    ws = format_workspace_context_block(source_cwd=source_cwd, target_cwd=target_cwd)
    return (
        "## Collaboration handoff\n\n"
        "You are continuing a **multi-walker** thread on one shared channel. Build on "
        "what the previous speaker did; add value from your role and provider.\n\n"
        f"- **Channel:** `{channel_id}`\n"
        f"- **Shared goal:** {goal.strip()}\n"
        f"- **Previous speaker:** {source_label} ({source_provider})\n"
        f"- **You are:** {target_label} ({target_provider})\n"
        f"- **Your role:** {target_role}\n"
        f"- **Turn:** {round_index} / {max_rounds}\n\n"
        + ws
        + "### Instructions\n\n"
        "- Extend the prior reply with your own analysis or implementation.\n"
        "- If the last message implies file or shell work, align paths with the cwd notes.\n"
        "- State outcomes clearly so the next turn (or the MrCat) can follow.\n"
        "- Stay concise; no meta-commentary about orchestration.\n\n"
        "### Previous speaker’s last message\n\n"
        f"{prior_response.strip()}\n"
    )
