from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from validate_skills import validate


class ValidateSkillsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.skill = self.root / "skills" / "sample"
        self.skill.mkdir(parents=True)
        self.entry = self.skill / "SKILL.md"
        self.entry.write_text("---\nname: sample\ndescription: Sample capability\n---\n# Sample\n", encoding="utf-8")

    def test_real_packaged_reference_is_valid(self):
        (self.skill / "details.md").write_text("Details", encoding="utf-8")
        self.entry.write_text(self.entry.read_text() + "[Details](details.md)\n", encoding="utf-8")
        self.assertEqual([], validate(self.root))

    def test_missing_packaged_reference_fails(self):
        self.entry.write_text(self.entry.read_text() + "[Details](details.md)\n", encoding="utf-8")
        self.assertIn("missing resource", "\n".join(validate(self.root)))

    def test_symlink_cannot_escape_independently_installed_package(self):
        outside = self.root / "outside.md"
        outside.write_text("Outside", encoding="utf-8")
        (self.skill / "details.md").symlink_to(outside)
        self.entry.write_text(self.entry.read_text() + "[Details](details.md)\n", encoding="utf-8")
        self.assertIn("escapes package", "\n".join(validate(self.root)))

    def test_name_must_match_installer_directory(self):
        self.entry.write_text("---\nname: other\ndescription: Sample capability\n---\n", encoding="utf-8")
        self.assertIn("name differs from directory", "\n".join(validate(self.root)))

    def test_default_prompt_matches_complete_skill_name(self):
        planner = self.root / "skills" / "sdlc-plan"
        interface = planner / "agents" / "openai.yaml"
        interface.parent.mkdir(parents=True)
        (planner / "SKILL.md").write_text(
            "---\nname: sdlc-plan\ndescription: Draft implementation plans.\n---\n",
            encoding="utf-8",
        )
        for skill, errors in (
            ("sdlc-plan-implementation", [f"{interface}: default prompt must name the skill"]),
            ("sdlc-plan", []),
        ):
            with self.subTest(skill=skill):
                interface.write_text(
                    'interface:\n  short_description: "Draft a reviewed implementation plan"\n'
                    f'  default_prompt: "Use ${skill}."\n',
                    encoding="utf-8",
                )
                self.assertEqual(errors, validate(self.root))


if __name__ == "__main__":
    unittest.main()
