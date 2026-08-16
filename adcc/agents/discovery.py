"""Agent capability discovery (P1).

Detects installed coding-agent CLIs on PATH and produces ready-to-use
adapter suggestions (no vendor SDKs, purely `shutil.which` probing).
"""

import os
import shutil

AGENT_PROBES = (
    ("opencode", "OpenCode"),
    ("codex", "Codex"),
    ("claude", "Claude Code"),
    ("gemini", "Gemini CLI"),
    ("aider", "Aider"),
    ("goose", "Goose"),
    ("qwen-code", "Qwen Code"),
    ("cursor-agent", "Cursor Agent"),
)

DEFAULT_ARGS = {
    "opencode": ["run", "--prompt-file", "{prompt_file}"],
    "codex": ["exec", "--json", "--prompt-file", "{prompt_file}"],
    "claude": ["-p", "--output-format", "json", "--output-file",
               "{prompt_file}"],
    "gemini": ["-p", "-q", "{prompt_file}"],
    "aider": ["--message-file", "{prompt_file}"],
    "goose": ["run", "-f", "{prompt_file}"],
    "qwen-code": ["-p", "{prompt_file}"],
    "cursor-agent": ["run", "-f", "{prompt_file}"],
}


def discover_agents():
    """Return installed agent suggestions: [{executable, label, path, args}]."""
    found = []
    for executable, label in AGENT_PROBES:
        path = shutil.which(executable)
        if path:
            found.append({
                "executable": executable,
                "label": label,
                "path": path,
                "argsTemplate": list(DEFAULT_ARGS.get(executable, [])),
            })
    return found


def suggest_adapter(executable):
    """Build a full adapter dict for an installed agent executable."""
    from adcc.agents.models import make_adapter
    for name, label in AGENT_PROBES:
        if name == executable:
            return make_adapter(
                name=label,
                executable=executable,
                args_template=list(DEFAULT_ARGS.get(executable, [])),
                env_template={
                    "ADCC_PROJECT_ID": "{project_id}",
                    "ADCC_SESSION_ID": "{session_id}",
                },
                stdin_mode="file",
            )
    return None
