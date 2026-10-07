"""evaluate_prompt コマンドの回帰テスト。

実際に API を叩くと費用が出るので、サービス層を差し替えて配線だけを見る。
"""

import io
import json
import pathlib
import re
import tempfile
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from .. import service
from ..evalset import dataset as evalset_dataset
from ..fixtures import FIXTURES
from ..management.commands import evaluate_prompt
from ..schema import RiskReportSchema
from .helpers import EVAL_CASES_TOML, eval_cases_toml


class EvalCommandTests(SimpleTestCase):
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
        with (
            mock.patch.object(evaluate_prompt.service, "job_offer_risk_assess") as api,
            self.assertRaisesMessage(CommandError, "VIEW_TEST_MODE"),
        ):
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

    @override_settings(VIEW_TEST_MODE=True)
    def test_the_plan_lists_every_category_even_at_zero(self):
        """0件のカテゴリも欄を出すこと。

        欄ごと消えると「0件」と書いてあるより気づきにくい。このテストセットは
        disguised を持っていないので、そのまま欠落の例になっている。
        """
        output = self._run(dry_run=True)

        self.assertRegex(output, r"disguised\s+0件")

    @override_settings(VIEW_TEST_MODE=True)
    def test_it_warns_when_a_dangerous_category_is_missing(self):
        """危険側が欠けていれば警告すること。

        見逃し率の分母は obvious と disguised の両方。片方が0件でも数字は出て
        しまうため、黙って通すと実態より良い値を実力として読んでしまう。
        """
        output = self._run(dry_run=True)

        self.assertIn("危険側のカテゴリが0件です", output)
        self.assertIn("disguised", output)

    @override_settings(VIEW_TEST_MODE=True)
    def test_it_stays_quiet_when_both_dangerous_categories_are_present(self):
        """危険側が揃っていれば警告しないこと。"""
        self.path.write_text(
            eval_cases_toml(obvious=1, disguised=1, legitimate=1), encoding="utf-8"
        )

        output = self._run(dry_run=True)

        self.assertNotIn("危険側のカテゴリが0件です", output)


@override_settings(VIEW_TEST_MODE=False)
class PromptOverrideTests(SimpleTestCase):
    """--prompt で判定プロンプトを差し替えられること。

    データセットが非公開で、外部の貢献者は評価を回せない。プロンプトを変える PR は
    こちらで測ってマージの可否を決めるので、候補と現行を、追跡下のファイルを
    書き換えずに比べられる必要がある。
    """

    def setUp(self):
        self.path = pathlib.Path(tempfile.mkdtemp()) / "cases.toml"
        self.path.write_text(EVAL_CASES_TOML, encoding="utf-8")
        self.report = RiskReportSchema.model_validate(FIXTURES["danger"])

    def _run(self, **kwargs):
        out = io.StringIO()
        call_command("evaluate_prompt", cases=self.path, stdout=out, **kwargs)
        return out.getvalue()

    def test_the_candidate_prompt_reaches_the_service(self):
        candidate = self.path.parent / "candidate.md"
        candidate.write_text("# 候補プロンプト", encoding="utf-8")

        with mock.patch.object(
            evaluate_prompt.service, "job_offer_risk_assess", return_value=self.report
        ) as api:
            self._run(yes=True, prompt=candidate)

        for call in api.call_args_list:
            self.assertEqual(call.kwargs["prompt_path"], candidate)

    def test_the_default_run_does_not_override_the_prompt(self):
        """既定では差し替えない（本番と同じプロンプトで測る）。"""
        with mock.patch.object(
            evaluate_prompt.service, "job_offer_risk_assess", return_value=self.report
        ) as api:
            self._run(yes=True)

        for call in api.call_args_list:
            self.assertIsNone(call.kwargs["prompt_path"])

    def test_a_missing_prompt_stops_before_any_api_call(self):
        """存在しないパスなら、1件も叩かずに止まること。

        104回まとめて失敗してから気づく、という壊れ方を避ける。
        """
        missing = self.path.parent / "not-here.md"

        with (
            mock.patch.object(evaluate_prompt.service, "job_offer_risk_assess") as api,
            self.assertRaisesMessage(CommandError, "プロンプトが見つかりません"),
        ):
            self._run(yes=True, prompt=missing)

        api.assert_not_called()

    def test_the_json_records_which_prompt_was_used(self):
        """どのプロンプトで測った数字かが残らないと、比較の記録にならない。"""
        candidate = self.path.parent / "candidate.md"
        candidate.write_text("# 候補プロンプト", encoding="utf-8")
        out_json = self.path.parent / "report.json"

        with mock.patch.object(
            evaluate_prompt.service, "job_offer_risk_assess", return_value=self.report
        ):
            self._run(yes=True, prompt=candidate, json=out_json)

        payload = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertEqual(payload["prompt"], str(candidate))

    def test_the_json_records_the_default_prompt_when_not_overridden(self):
        out_json = self.path.parent / "report.json"

        with mock.patch.object(
            evaluate_prompt.service, "job_offer_risk_assess", return_value=self.report
        ):
            self._run(yes=True, json=out_json)

        payload = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertEqual(payload["prompt"], str(service.PROMPT_PATH))

    def test_the_service_sends_the_prompt_it_was_given(self):
        """差し替えたファイルの中身が、実際に system として送られること。

        コマンド側の配線だけ見ていても、service が受け取った値を使わなければ
        意味がないので、ここだけ API 直前まで見る。
        """
        candidate = self.path.parent / "candidate.md"
        candidate.write_text("# 候補プロンプト\nここだけ違う", encoding="utf-8")

        with mock.patch.object(service.anthropic, "Anthropic") as client_class:
            parse = client_class.return_value.messages.parse
            parse.return_value = mock.Mock(stop_reason="end_turn")
            service.job_offer_risk_assess("日給5万円", prompt_path=candidate)

        self.assertIn("ここだけ違う", parse.call_args.kwargs["system"][0]["text"])

    def test_a_missing_candidate_prompt_fails_without_calling_the_api(self):
        """読めないプロンプトなら、API に投げずに落とすこと（費用だけ出るため）。"""
        missing = self.path.parent / "not-here.md"

        with mock.patch.object(service.anthropic, "Anthropic") as client_class:
            parse = client_class.return_value.messages.parse
            result = service.job_offer_risk_assess("日給5万円", prompt_path=missing)

        self.assertIs(result, service.AssessmentError.FAILED)
        parse.assert_not_called()


@override_settings(VIEW_TEST_MODE=False)
class EvalJsonReportTests(SimpleTestCase):
    """評価結果の JSON 書き出しの検証。

    モデルやプロンプトを変えたときの比較は、この JSON を後から読み返して
    行う（.internal/.reports/ に置く運用）。指標だけでなく判定の文面まで残す一方で、
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


@override_settings(VIEW_TEST_MODE=False)
class RunSizeMatchesThePlanTests(SimpleTestCase):
    """実行回数の見積もりと、実際に API を叩く回数が一致することの検証。

    評価は実行のたびに費用が出るため、測り直しが効かない。画面に出た回数と
    実際の回数がずれると、費用の判断も、どれだけの母数で読むべきかも狂う。
    """

    def setUp(self):
        self.directory = pathlib.Path(tempfile.mkdtemp())
        self.report = RiskReportSchema.model_validate(FIXTURES["danger"])

    def _write(self, **counts):
        path = self.directory / "cases.toml"
        path.write_text(eval_cases_toml(**counts), encoding="utf-8")
        return path

    def _run(self, path, **options):
        out = io.StringIO()
        with mock.patch.object(
            evaluate_prompt.service, "job_offer_risk_assess", return_value=self.report
        ) as api:
            call_command("evaluate_prompt", cases=path, yes=True, stdout=out, **options)
        return api.call_count, out.getvalue()

    def _planned_runs(self, output):
        """画面に出た「判定の実行回数」を読み取る。"""
        found = re.search(r"判定の実行回数 (\d+)", output)
        self.assertIsNotNone(found, output)
        return int(found.group(1))

    def test_limit_caps_each_category(self):
        path = self._write(obvious=3, legitimate=3, gray=3)

        calls, output = self._run(path, limit=2)

        # 各カテゴリ2件まで。グレーだけ既定で繰り返す
        self.assertEqual(calls, 2 + 2 + 2 * evaluate_prompt.DEFAULT_GRAY_REPEAT)
        self.assertEqual(self._planned_runs(output), calls)

    def test_repeat_applies_to_every_category(self):
        path = self._write(obvious=1, legitimate=1, gray=1)

        calls, output = self._run(path, repeat=2)

        self.assertEqual(calls, 3 * 2)
        self.assertEqual(self._planned_runs(output), calls)

    def test_repeat_replaces_the_gray_default(self):
        """--repeat を渡したら、グレーもその回数になること（既定に戻らない）。"""
        path = self._write(gray=1)

        calls, _ = self._run(path, repeat=2)

        self.assertEqual(calls, 2)
        self.assertNotEqual(calls, evaluate_prompt.DEFAULT_GRAY_REPEAT)

    def test_the_estimate_matches_the_run_for_every_combination(self):
        """--dry-run で見た回数のまま実行されること。

        見積もりを見て実行を決めるので、ここがずれると判断の前提が崩れる。
        """
        path = self._write(obvious=3, disguised=2, legitimate=3, gray=2)
        combinations = (
            {},
            {"limit": 1},
            {"repeat": 2},
            {"category": ["gray"]},
            {"category": ["obvious", "gray"], "limit": 2, "repeat": 3},
        )
        for options in combinations:
            with self.subTest(options=options):
                _, planned = self._run(path, dry_run=True, **options)
                calls, _ = self._run(path, **options)

                self.assertEqual(calls, self._planned_runs(planned))


@override_settings(VIEW_TEST_MODE=False)
class SpendingNeedsConfirmationTests(SimpleTestCase):
    """実行前の確認の検証。

    ここで止まらないと、意図しない実行の費用がそのまま出る。
    """

    def setUp(self):
        self.path = pathlib.Path(tempfile.mkdtemp()) / "cases.toml"
        self.path.write_text(EVAL_CASES_TOML, encoding="utf-8")
        self.report = RiskReportSchema.model_validate(FIXTURES["danger"])

    def _run_with_answer(self, answer, **options):
        out = io.StringIO()
        with (
            mock.patch.object(
                evaluate_prompt.service,
                "job_offer_risk_assess",
                return_value=self.report,
            ) as api,
            mock.patch("builtins.input", return_value=answer) as prompt,
        ):
            call_command("evaluate_prompt", cases=self.path, stdout=out, **options)
        return api, prompt, out.getvalue()

    def test_answering_no_stops_before_any_api_call(self):
        """はっきり yes と答えない限り、API は叩かないこと。"""
        for answer in ("n", "N", "", "  ", "いいえ"):
            with self.subTest(answer=repr(answer)):
                api, _, output = self._run_with_answer(answer)

                api.assert_not_called()
                self.assertIn("中止しました", output)

    def test_answering_yes_runs_the_evaluation(self):
        for answer in ("y", "Y", "yes", " YES "):
            with self.subTest(answer=repr(answer)):
                api, _, output = self._run_with_answer(answer)

                self.assertEqual(
                    api.call_count, 2 + evaluate_prompt.DEFAULT_GRAY_REPEAT
                )
                self.assertIn("評価結果", output)

    def test_the_prompt_says_how_many_runs_and_how_much(self):
        """確認の時点で、回数と費用が分かること。"""
        _, prompt, _ = self._run_with_answer("n")

        asked = prompt.call_args[0][0]
        runs = 2 + evaluate_prompt.DEFAULT_GRAY_REPEAT
        self.assertIn(f"{runs} 回", asked)
        self.assertIn(f"${runs * evaluate_prompt.COST_PER_CASE_USD:.2f}", asked)

    def test_the_confirmation_is_skipped_when_it_is_given_up_front(self):
        """--yes を渡したときは、確認を出さずに実行すること。"""
        out = io.StringIO()
        with (
            mock.patch.object(
                evaluate_prompt.service,
                "job_offer_risk_assess",
                return_value=self.report,
            ),
            mock.patch("builtins.input") as prompt,
        ):
            call_command("evaluate_prompt", cases=self.path, yes=True, stdout=out)

        prompt.assert_not_called()
