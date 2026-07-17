from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SkillContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        cls.prompting = (ROOT / "references" / "prompting.md").read_text(encoding="utf-8")
        cls.samples = (ROOT / "references" / "sample-prompts.md").read_text(encoding="utf-8")
        cls.cli = (ROOT / "references" / "cli.md").read_text(encoding="utf-8")
        cls.safety_notes = (ROOT / "references" / "review-and-test-notes.md").read_text(encoding="utf-8")
        cls.agent = (ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")

    def test_configuration_is_two_environment_variables(self):
        self.assertIn("DDW_IMAGE_API_KEY", self.skill)
        self.assertIn("DDW_IMAGE_BASE_URL", self.skill)
        self.assertIn("DDW_IMAGE_API_KEY", self.cli)
        self.assertIn("DDW_IMAGE_BASE_URL", self.cli)

    def test_normal_flow_is_natural_language_one_shot(self):
        for phrase in ("generate", "edit", "composite", "project-bound", "preview-only"):
            self.assertIn(phrase, self.skill.lower())
        self.assertIn("create --prompt", self.skill)
        self.assertIn("Do not narrate", self.skill)

    def test_paid_count_and_concurrency_are_explicit(self):
        self.assertIn("--n", self.skill)
        self.assertIn("exactly", self.skill.lower())
        self.assertIn("at most two", self.skill.lower())
        self.assertIn("second paid", self.skill.lower())

    def test_transparency_has_preflight_and_postprocess(self):
        skill = self.skill.lower()
        for phrase in ("chroma-key", "remove_chroma_key.py", "hair", "glass", "before any paid submit"):
            self.assertIn(phrase, skill)

    def test_complex_transparency_stops_before_paid_submit(self):
        section = self.skill.split("## Transparency preflight", 1)[1].split("## Deliver", 1)[0].lower()
        for phrase in (
            "fur",
            "feathers",
            "smoke",
            "glass",
            "liquids",
            "translucent",
            "reflective",
            "soft shadows",
            "do not submit",
            "ask the user",
            "no native transparent-background control",
            "remove_chroma_key.py --check",
        ):
            self.assertIn(phrase, section)

    def test_project_delivery_is_closed_loop(self):
        for phrase in ("absolute path", "versioned", "consuming code", "workspace", "read back", "resolves to the delivered file"):
            self.assertIn(phrase, self.skill.lower())

    def test_failures_are_actionable(self):
        for phrase in ("what happened", "paid submit", "safe next action"):
            self.assertIn(phrase, self.skill.lower())

    def test_prompt_and_sample_references_cover_common_jobs(self):
        for phrase in ("product-mockup", "ads-marketing", "identity-preserve", "precise-object-edit", "compositing"):
            self.assertIn(phrase, self.samples)
        self.assertIn("Preserve exactly", self.prompting)

    def test_agent_metadata_promises_simple_end_to_end_use(self):
        self.assertIn("DDW", self.agent)
        self.assertIn("end to end", self.agent.lower())
        self.assertIn("natural", self.agent.lower())

    def test_safety_reference_is_current_not_a_version_log(self):
        notes = self.safety_notes.lower()
        for phrase in ("## v4", "## v5", "## v6", "production multipart edit incident"):
            self.assertNotIn(phrase, notes)
        self.assertLessEqual(len(self.safety_notes.splitlines()), 80)
