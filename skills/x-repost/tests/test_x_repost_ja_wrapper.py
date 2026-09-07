import pathlib
import json
import unittest


ROOT = pathlib.Path(__file__).parents[3]
WRAPPER = ROOT / "skills/x-repost/x-repost-ja-cli.sh"
MAIN = ROOT / "skills/x-repost/x-repost-cli.sh"
HEALTHCHECK = ROOT / "skills/x-repost/x-repost-healthcheck.sh"
DIGEST = ROOT / "skills/x-repost/x-repost-digest.sh"


class JapaneseDiceLoopContractTests(unittest.TestCase):
    def test_wrapper_owns_dice_identity_and_japanese_any_source_policy(self):
        text = WRAPPER.read_text()
        for contract in (
            'X_REPOST_BROWSER_IDENTITY="x:diceai0"',
            'X_REPOST_ACCOUNT_HANDLE="@diceai0"',
            'X_REPOST_EXPECTED_HANDLE="diceai0"',
            'X_REPOST_FORCE_LANGUAGE="ja"',
            'X_REPOST_SOURCE_LANGUAGE_POLICY="any"',
            'X_REPOST_QUERIES_FILE=',
            'X_REPOST_FORCE_KIND="quote"',
            'X_REPOST_PUBLISH_TRANSPORT="browser"',
        ):
            self.assertIn(contract, text)

    def test_prompt_allows_english_source_but_requires_japanese_output(self):
        text = MAIN.read_text()
        self.assertIn("日本語・英語どちらの候補も選べる", text)
        self.assertIn("英語sourceは自然な日本語の付加価値へ翻訳", text)
        self.assertIn("アカウント固有設定の関心領域", text)
        self.assertIn('registry_enforce_or_exit "$LOOP_NAME"', text)
        self.assertIn('--loop "${LIFE_MANAGER_LOOP_ID:-$LOOP_NAME}"', text)

    def test_launchd_contract_is_half_hourly_and_offset(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        loop = registry["loops"]["x-repost-ja-pass"]
        self.assertEqual(loop["cadence"]["calendar_interval"], [{"Minute": 5}, {"Minute": 35}])
        health = registry["loops"]["x-repost-ja-healthcheck"]
        self.assertEqual(health["label"], "ai.anicca.x-repost-ja-healthcheck")

    def test_clean_registry_healthchecks_select_their_pass_and_state(self):
        text = HEALTHCHECK.read_text()
        self.assertIn('LIFE_MANAGER_LOOP_ID:-', text)
        self.assertIn('DEFAULT_LABEL="ai.anicca.x-repost-ja-pass"', text)
        self.assertIn('DEFAULT_STATE="$HOME/loops/x-repost-ja"', text)
        self.assertIn("DEFAULT_MAX_AGE_SECONDS=5400", text)
        self.assertIn("DEFAULT_INITIAL_GRACE_SECONDS=3600", text)
        self.assertIn('DEFAULT_LABEL="ai.anicca.x-repost-pass"', text)
        self.assertIn('DEFAULT_STATE="$HOME/loops/x-repost-en"', text)

    def test_digest_defaults_to_the_english_repost_state(self):
        self.assertIn('$HOME/loops/x-repost-en', DIGEST.read_text())

    def test_runtime_dependencies_are_repository_owned(self):
        main = MAIN.read_text()
        digest = DIGEST.read_text()
        self.assertIn('skills/_shared/send-telegram.sh', main)
        self.assertIn('skills/_shared/send-telegram.sh', digest)
        self.assertIn('$SKILL/config/humanize-checklist.md', main)
        self.assertIn('.local/state/life-manager/.env', main)
        self.assertNotIn('openclaw message send', main + digest)
        self.assertNotIn('$HOME/' + '.openclaw', main + digest)


if __name__ == "__main__":
    unittest.main()
