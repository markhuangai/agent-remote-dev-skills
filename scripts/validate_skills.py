#!/usr/bin/env python3
"""Validate discoverable skill metadata and self-contained packaged resources."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml


ROOT = Path(__file__).resolve().parents[1]
FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)
LINK = re.compile(r"!?\[[^\]\n]*\]\(([^)\n]+)\)")


def resource_errors(document: Path, boundary: Path) -> list[str]:
    errors = []
    fenced = False
    for line in document.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        if fenced:
            continue
        for match in LINK.finditer(line):
            target = match.group(1).strip().strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or not parsed.path:
                continue
            path = (document.parent / unquote(parsed.path)).resolve()
            if not path.is_relative_to(boundary.resolve()):
                errors.append(f"{document}: resource escapes package: {target}")
            elif not path.exists():
                errors.append(f"{document}: missing resource: {target}")
    return errors


def validate(root: Path) -> list[str]:
    errors = []
    skills = sorted((root / "skills").glob("*/SKILL.md"))
    if not skills:
        return [f"{root}: no skills discovered"]
    names = set()
    for entry in skills:
        match = FRONTMATTER.match(entry.read_text(encoding="utf-8"))
        if not match:
            errors.append(f"{entry}: missing YAML frontmatter")
            continue
        try:
            metadata = yaml.safe_load(match.group(1))
        except yaml.YAMLError as error:
            errors.append(f"{entry}: invalid YAML: {error}")
            continue
        if not isinstance(metadata, dict):
            errors.append(f"{entry}: frontmatter must be a mapping")
            continue
        name = metadata.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
            errors.append(f"{entry}: invalid skill name")
        elif name != entry.parent.name or name in names:
            errors.append(f"{entry}: duplicate name or name differs from directory")
        else:
            names.add(name)
        description = metadata.get("description")
        if not isinstance(description, str) or not description.strip() or len(description) > 1024:
            errors.append(f"{entry}: description must contain 1–1024 characters")
        for document in sorted(entry.parent.rglob("*.md")):
            errors.extend(resource_errors(document, entry.parent))
        interface_file = entry.parent / "agents" / "openai.yaml"
        if interface_file.exists():
            try:
                config = yaml.safe_load(interface_file.read_text(encoding="utf-8"))
                interface = config["interface"]
                short = interface["short_description"]
                if not isinstance(short, str) or not 25 <= len(short) <= 64:
                    errors.append(f"{interface_file}: short description must contain 25–64 characters")
                default_prompt = interface.get("default_prompt")
                if not isinstance(default_prompt, str) or name not in re.findall(r"\$([\w-]+)", default_prompt):
                    errors.append(f"{interface_file}: default prompt must name the skill")
            except (yaml.YAMLError, KeyError, TypeError) as error:
                errors.append(f"{interface_file}: invalid interface metadata: {error}")
    if (root / "README.md").exists():
        errors.extend(resource_errors(root / "README.md", root))
    return errors


def main() -> int:
    errors = validate(ROOT)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"Validated {len(list((ROOT / 'skills').glob('*/SKILL.md')))} skills and their resource links.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
