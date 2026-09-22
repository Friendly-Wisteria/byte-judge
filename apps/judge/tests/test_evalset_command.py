"""evaluate_prompt コマンドの回帰テスト。

実際に API を叩くと費用が出るので、サービス層を差し替えて配線だけを見る。
"""

import io
import json
import pathlib
import tempfile
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from .. import service
from ..evalset import dataset as evalset_dataset
from ..fixtures import FIXTURES
from ..management.commands import evaluate_prompt
from ..schema import RiskReportSchema
from .helpers import EVAL_CASES_TOML


class EvalCommandTests(TestCase):
    """evaluate_prompt コマンドの検証。

    実際に API を叩くと費用が出るので、サービス層を差し替えて配線だけを見る。
    """


    def setUp(self):
        self.path = pathlib.Path(tempfile.mkdtemp()) / "cases.toml"
        self.path.write_text(EVAL_CASES_TOML, encoding="utf-8")

    def _run(self, **kwargs):
        out = io.StringIO()
        call_command("evaluate_prompt", cases=self.path, stdout=out, **kwargs)
        return out.getvalue()

    @override_settings(VIEW_TEST_MODE=True)
    def test_dry_run_shows_the_plan_without_calling_the_api(self):
        """--dry-run は API を叩かず、構成と見積もりだけ出すこと。"""
        with mock.patch.object(evaluate_prompt.service, "job_offer_risk_assess") as api:
            output = self._run(dry_run=True)

        api.assert_not_called()
        self.assertIn("ケース数 3", output)
        self.assertIn("費用の見積もり", output)

    @override_settings(VIEW_TEST_MODE=True)
    def test_it_refuses_to_run_while_view_test_mode_is_on(self):
        """固定サンプルが返る状態で評価すると、結果が意味を失う。"""
        with mock.patch.object(evaluate_prompt.service, "job_offer_risk_assess") as api:
            with self.assertRaisesMessage(CommandError, "VIEW_TEST_MODE"):
                self._run(yes=True)

        api.assert_not_called()

    @override_settings(VIEW_TEST_MODE=False, CLAUDE_MODEL="claude-test-9")
    def test_the_report_carries_the_model_and_the_metrics(self):
        report = RiskReportSchema.model_validate(FIXTURES["danger"])
        with mock.patch.object(
            evaluate_prompt.service, "job_offer_risk_assess", return_value=report
        ):
            output = self._run(yes=True)

        self.assertIn("claude-test-9", output)
        self.assertIn("危険求人の見逃し率", output)
        self.assertIn("グレー求人への判定の安定性", output)
        self.assertIn("シグナル理由の妥当性", output)

    @override_settings(VIEW_TEST_MODE=False)
    def test_gray_cases_are_repeated_by_default(self):
        """安定性を測るため、グレーだけ既定で繰り返すこと。"""
        report = RiskReportSchema.model_validate(FIXTURES["danger"])
        with mock.patch.object(
            evaluate_prompt.service, "job_offer_risk_assess", return_value=report
        ) as api:
            self._run(yes=True)

        # obvious 1 + legitimate 1 + gray 1×3
        self.assertEqual(api.call_count, 2 + evaluate_prompt.DEFAULT_GRAY_REPEAT)

    @override_settings(VIEW_TEST_MODE=False)
    def test_an_api_failure_is_counted_but_does_not_stop_the_run(self):
        with mock.patch.object(
            evaluate_prompt.service,
            "job_offer_risk_assess",
            return_value=service.AssessmentError.UNAVAILABLE,
        ):
            output = self._run(yes=True)

        self.assertIn("判定できなかった理由", output)
        self.assertIn("UNAVAILABLE", output)
        # 見逃し率の分母から外れるので、割合は出ない
        self.assertIn("対象なし", output)

    @override_settings(VIEW_TEST_MODE=True)
    def test_a_category_filter_narrows_the_run(self):
        output = self._run(dry_run=True, category=["legitimate"])

        self.assertIn("ケース数 1", output)


@override_settings(VIEW_TEST_MODE=False)
class EvalJsonReportTests(TestCase):
    """評価結果の JSON 書き出しの検証。

    モデルやプロンプトを変えたときの比較は、この JSON を後から読み返して
    行う（.reports/ に置く運用）。指標だけでなく判定の文面まで残す一方で、
    テストセットの本文は書き出さない、という線引きをここで固定する。
    """

    def setUp(self):
        directory = pathlib.Path(tempfile.mkdtemp())
        self.cases_path = directory / "cases.toml"
        self.cases_path.write_text(EVAL_CASES_TOML, encoding="utf-8")
        self.json_path = directory / "result.json"
        self.report = RiskReportSchema.model_validate(FIXTURES["danger"])

    def _run(self):
        out = io.StringIO()
        with mock.patch.object(
            evaluate_prompt.service, "job_offer_risk_assess", return_value=self.report
        ):
            call_command(
                "evaluate_prompt",
                cases=self.cases_path,
                json=self.json_path,
                yes=True,
                stdout=out,
            )
        return json.loads(self.json_path.read_text(encoding="utf-8")), out.getvalue()

    def test_the_json_holds_the_run_and_the_metrics(self):
        payload, output = self._run()

        self.assertEqual(payload["model"], settings.CLAUDE_MODEL)
        self.assertEqual(payload["cases"], 3)
        self.assertEqual(payload["runs"], 2 + evaluate_prompt.DEFAULT_GRAY_REPEAT)
        # 危険側の1件を「危険」と判定できているので、見逃しは 0
        self.assertEqual(payload["false_negative_rate"], 0.0)
        self.assertEqual(payload["gray_label_agreement"], 1.0)
        self.assertIn(str(self.json_path), output)

    def test_each_outcome_keeps_the_model_wording(self):
        """後から人が読み返せるよう、判定の文面まで残っていること。"""
        payload, _ = self._run()

        entry = payload["outcomes"][0]
        self.assertEqual(
            sorted(entry),
            sorted([
                "case_id", "category", "error", "level", "label", "score",
                "has_enough_info", "signal_text", "summary", "advice",
            ]),
        )
        self.assertEqual(entry["summary"], self.report.summary)
        self.assertEqual(entry["advice"], self.report.advice)
        self.assertIn("異常な高額報酬", entry["signal_text"])

    def test_the_json_does_not_copy_the_case_text(self):
        """テストセットの本文は書き出さないこと。

        危険側は合成データだが、よくできているほど募集文のテンプレートとして
        使えてしまうため、データ本体はリポジトリから外している
        （apps/judge/evalset/README.md）。書き出し先に本文が写ると、その
        判断が無意味になる。
        """
        self._run()
        raw = self.json_path.read_text(encoding="utf-8")

        for case in evalset_dataset.load_cases(self.cases_path):
            with self.subTest(case=case.id):
                self.assertNotIn(case.text, raw)
                self.assertIn(case.id, raw)  # どのケースの結果かは辿れる
