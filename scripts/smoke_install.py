#!/usr/bin/env python3
"""Exercise real skill discovery and installation without global installs."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ("sdlc-plan-implementation", "plan-audit", "pr-review-loop")
AGENT_PATHS = {"codex": ".agents/skills", "claude-code": ".claude/skills", "opencode": ".agents/skills"}


def main() -> None:
    environment = {**os.environ, "DISABLE_TELEMETRY": "1", "NO_COLOR": "1"}
    with tempfile.TemporaryDirectory(prefix="skill-marketplace-install-") as directory:
        workspace = Path(directory)
        discovery = subprocess.run(["npx", "--yes", "skills@1.8.1", "add", str(ROOT), "--list"],
                                   cwd=workspace, env=environment, text=True, capture_output=True,
                                   check=True, timeout=180)
        for name in SKILLS:
            if name not in discovery.stdout:
                raise RuntimeError(f"Installer did not discover {name}: {discovery.stdout}")
        for agent, relative in AGENT_PATHS.items():
            project = workspace / agent
            project.mkdir()
            command = ["npx", "--yes", "skills@1.8.1", "add", str(ROOT), "--skill", "*",
                       "--agent", agent, "--copy", "--yes"]
            result = subprocess.run(command, cwd=project, env=environment, text=True,
                                    capture_output=True, check=True, timeout=180)
            for name in SKILLS:
                source = ROOT / "skills" / name
                installed = project / relative / name
                for resource in source.rglob("*"):
                    if not resource.is_file() or "__pycache__" in resource.parts:
                        continue
                    copied = installed / resource.relative_to(source)
                    if not copied.is_file() or copied.read_bytes() != resource.read_bytes():
                        raise RuntimeError(f"{agent}: missing or changed {copied}; installer output: {result.stdout}")
            print(f"Installed and verified all {len(SKILLS)} skills for {agent}.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(error.stdout or "", file=sys.stderr)
        print(error.stderr or "", file=sys.stderr)
        raise
