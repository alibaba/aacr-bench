import os.path

from claude_agent_sdk import ClaudeAgentOptions
import json


def load_config() -> dict:
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(project_root, "configs", "config.json")
    with open(config_path, "r") as f:
        return json.load(f)

def get_claude_code_options(workspace: str, allowed_tools: list) -> ClaudeAgentOptions:
    # cli_path read from config.json
    config = load_config()
    return ClaudeAgentOptions(
        allowed_tools=allowed_tools,
        cli_path=config.get("cli_path"),
        cwd=workspace
    )

def add_code_review_agent(workspace: str, agent_md: str):
    """
    Create .claude/agents/code-reviewer.md in workspace
    """
    agent_path = os.path.join(workspace, ".claude/agents")
    if not os.path.exists(agent_path):
        os.makedirs(agent_path)
    agent_path = os.path.join(agent_path, "code-reviewer.md")
    with open(agent_path, "w", encoding="utf-8") as f:
        f.write(agent_md)
