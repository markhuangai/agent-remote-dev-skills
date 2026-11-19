#!/usr/bin/env python3
"""Exercise real skill discovery and installation without global installs."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ("sdlc-plan", "sdlc-plan-implementation", "plan-audit", "pr-review-loop")
AGENT_PATHS = {"codex": ".agents/skills", "claude-code": ".claude/skills", "opencode": ".agents/skills"}


def install_and_verify(
    project: Path, agent: str, selector: str, expected_skills: tuple[str, ...], environment: dict[str, str]
) -> None:
    project.mkdir()
    command = ["npx", "--yes", "skills@1.8.1", "add", str(ROOT), "--skill", selector,
               "--agent", agent, "--copy", "--yes"]
    result = subprocess.run(command, cwd=project, env=environment, text=True,
                            capture_output=True, check=True, timeout=180)
    installed = project / AGENT_PATHS[agent]
    actual_skills = {entry.parent.name for entry in installed.glob("*/SKILL.md")}
    if actual_skills != set(expected_skills):
        raise RuntimeError(
            f"{project.name}: expected {sorted(expected_skills)}, found {sorted(actual_skills)}; "
            f"installer output: {result.stdout}"
        )
    for name in expected_skills:
        source = ROOT / "skills" / name
        for resource in source.rglob("*"):
            if not resource.is_file() or "__pycache__" in resource.parts:
                continue
            copied = installed / name / resource.relative_to(source)
            if not copied.is_file() or copied.read_bytes() != resource.read_bytes():
                raise RuntimeError(f"{project.name}: missing or changed {copied}; installer output: {result.stdout}")
    print(f"{project.name}: installed and verified {', '.join(expected_skills)}.", flush=True)


def main() -> None:
    environment = {**os.environ, "DISABLE_TELEMETRY": "1", "NO_COLOR": "1"}
    with tempfile.TemporaryDirectory(prefix="skill-marketplace-install-") as directory:
        workspace = Path(directory)
        discovery = subprocess.run(["npx", "--yes", "skills@1.8.1", "add", str(ROOT), "--list"],
                                   cwd=workspace, env=environment, text=True, capture_output=True,
                                   check=True, timeout=180)
        discovered = set(re.findall(r"^[│|] {4}([a-z0-9-]+)[ \t]*$", discovery.stdout, re.MULTILINE))
        missing = set(SKILLS) - discovered
        if missing:
            raise RuntimeError(f"Installer did not discover {sorted(missing)}: {discovery.stdout}")
        for agent in AGENT_PATHS:
            install_and_verify(workspace / agent, agent, "*", SKILLS, environment)
            install_and_verify(workspace / f"{agent}-planner", agent, "sdlc-plan", ("sdlc-plan",), environment)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(error.stdout or "", file=sys.stderr)
        print(error.stderr or "", file=sys.stderr)
        raise
