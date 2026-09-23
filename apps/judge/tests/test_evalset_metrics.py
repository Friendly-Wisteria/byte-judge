"""評価指標の計算の回帰テスト。

ここを間違えると、改善したかどうかの判断そのものを誤る。
"""

from django.test import SimpleTestCase

from ..evalset import dataset as evalset_dataset
from ..evalset import metrics as evalset_metrics
from ..fixtures import FIXTURES
from ..schema import Level, RiskReportSchema


class EvalMetricsTests(SimpleTestCase):
    """指標の計算の検証。

    ここを間違えると、改善したかどうかの判断そのものを誤る。とくに見逃し率は
    最重視する指標なので、何を見逃しに数えるかを固定しておく。
    """

    def _make_outcome(self, category, level=None, enough=True, error=None, **kwargs):
        return evalset_metrics.Outcome(
            case_id=kwargs.pop("case_id", "c1"),
            category=category,
            level=level,
            label=kwargs.pop("label", None),
            score=kwargs.pop("score", None),
            has_enough_info=enough,
            error=error,
            signal_text=kwargs.pop("signal_text", ""),
        )

    def test_a_safe_verdict_on_a_dangerous_case_is_a_miss(self):
        result = evalset_metrics.false_negative(
            [self._make_outcome("obvious", level=Level.SAFE)]
        )

        self.assertEqual(result.rate, 1.0)
        self.assertEqual(result.detail["危険な兆候なしと判定"], 1)

    def test_insufficient_info_on_a_dangerous_case_is_also_a_miss(self):
        """情報不足も見逃しに数えること（画面上は警告が出ていないため）。"""
        result = evalset_metrics.false_negative(
            [self._make_outcome("obvious", level=Level.DANGER, enough=False)]
        )

        self.assertEqual(result.rate, 1.0)
        self.assertEqual(result.detail["情報不足で判定を出せず"], 1)

    def test_a_caution_verdict_is_not_a_miss(self):
        """要注意は警告が届いているので、見逃しには数えない。"""
        result = evalset_metrics.false_negative(
            [self._make_outcome("disguised", level=Level.CAUTION)]
        )

        self.assertEqual(result.rate, 0.0)

    def test_only_dangerous_categories_count_towards_the_miss_rate(self):
        result = evalset_metrics.false_negative(
            [
                self._make_outcome("legitimate", level=Level.SAFE),
                self._make_outcome("gray", level=Level.SAFE),
            ]
        )

        self.assertEqual(result.total, 0)
        self.assertIsNone(result.rate)

    def test_errors_are_excluded_from_the_denominator(self):
        """判定を受け取れなかった分は、モデルの見逃しとして数えない。"""
        result = evalset_metrics.false_negative(
            [
                self._make_outcome("obvious", error="UNAVAILABLE"),
                self._make_outcome("obvious", level=Level.DANGER),
            ]
        )

        self.assertEqual(result.total, 1)
        self.assertEqual(result.count, 0)

    def test_stability_notices_a_split_verdict(self):
        outcomes = [
            self._make_outcome("gray", case_id="g1", label="要注意", score=40),
            self._make_outcome("gray", case_id="g1", label="情報不足", score=20),
            self._make_outcome("gray", case_id="g1", label="要注意", score=42),
        ]

        result = evalset_metrics.stability(outcomes, category="gray")

        self.assertEqual(result.cases, 1)
        self.assertAlmostEqual(result.label_agreement, 2 / 3)
        self.assertEqual(result.split_cases, ["g1"])
        self.assertGreater(result.score_stdev, 0)

    def test_stability_ignores_cases_run_only_once(self):
        result = evalset_metrics.stability(
            [self._make_outcome("gray", case_id="g1", label="要注意")], category="gray"
        )

        self.assertEqual(result.cases, 0)
        self.assertIn("対象なし", result.format())

    def test_signal_recall_counts_the_expected_grounds(self):
        cases = [
            evalset_dataset.Case(
                id="c1",
                category="obvious",
                text="x",
                expect_signals=("高額報酬|高すぎる報酬", "Telegram"),
            )
        ]
        outcomes = [self._make_outcome("obvious", signal_text="高すぎる報酬 日給5万円")]

        result = evalset_metrics.signal_recall(cases, outcomes)

        # 言い回しが違っても、"|" で並べた言い換えのどれかに当たれば拾う
        self.assertEqual((result.count, result.total), (1, 2))
        self.assertEqual(result.detail["c1"], ["Telegram"])

    def test_signal_recall_reports_a_miss_by_its_first_wording(self):
        cases = [
            evalset_dataset.Case(
                id="c1", category="obvious", text="x",
                expect_signals=("秘匿アプリ|Telegram|Signal",),
            )
        ]

        result = evalset_metrics.signal_recall(
            cases, [self._make_outcome("obvious", signal_text="高すぎる報酬")]
        )

        self.assertEqual(result.detail["c1"], ["秘匿アプリ"])

    def test_a_ratio_with_no_target_does_not_report_zero_percent(self):
        """分母が0のときに 0% と出すと、良い成績と読めてしまう。"""
        self.assertEqual(evalset_metrics.Ratio(0, 0).format(), "対象なし")

    # ---- 参考値の指標（モデルを変えるときの比較材料になる） --------------

    def test_an_obvious_case_left_at_caution_is_counted_as_underrated(self):
        result = evalset_metrics.underrated_obvious([
            self._make_outcome("obvious", level=Level.CAUTION),
            self._make_outcome("obvious", level=Level.DANGER, case_id="c2"),
        ])

        self.assertEqual((result.count, result.total), (1, 2))

    def test_only_obvious_cases_count_as_underrated(self):
        """disguised は要注意が妥当なこともあるので、分母に入れない。"""
        result = evalset_metrics.underrated_obvious([
            self._make_outcome("disguised", level=Level.CAUTION),
        ])

        self.assertEqual(result.total, 0)
        self.assertIsNone(result.rate)

    def test_an_obvious_case_without_enough_info_is_not_underrated(self):
        """情報不足は見逃しに数えるので、参考値の側で二重に数えない。"""
        result = evalset_metrics.underrated_obvious([
            self._make_outcome("obvious", level=Level.CAUTION, enough=False),
        ])

        self.assertEqual((result.count, result.total), (0, 1))

    def test_a_legitimate_case_judged_dangerous_is_a_false_positive(self):
        result = evalset_metrics.false_positive([
            self._make_outcome("legitimate", level=Level.DANGER),
            self._make_outcome("legitimate", level=Level.CAUTION, case_id="c2"),
        ])

        self.assertEqual((result.count, result.total), (1, 2))

    def test_only_legitimate_cases_count_as_false_positives(self):
        """グレーを危険と判定しても、偽陽性とは言えない。"""
        result = evalset_metrics.false_positive([
            self._make_outcome("gray", level=Level.DANGER),
        ])

        self.assertEqual(result.total, 0)

    def test_errors_are_excluded_from_the_reference_metrics(self):
        """判定を受け取れなかった回は、どちらの分母にも入れない。"""
        errored = [
            self._make_outcome("obvious", error="UNAVAILABLE"),
            self._make_outcome("legitimate", error="UNAVAILABLE", case_id="c2"),
        ]

        self.assertEqual(evalset_metrics.underrated_obvious(errored).total, 0)
        self.assertEqual(evalset_metrics.false_positive(errored).total, 0)


class EvalOutcomeKeepsTheWordingTests(SimpleTestCase):
    """判定結果を評価用の Outcome に写す処理の検証。

    指標に出ない品質（日本語の読みやすさ・口調）は、後から人が読むしかない。
    そのために summary と advice を持たせているので、写し漏れがないことを
    固定する。評価用テストセットは合成データで、利用者の入力ではない
    （だから持ってよい）。
    """

    def _case(self, **kwargs):
        return evalset_dataset.Case(
            id=kwargs.get("id", "o1"),
            category=kwargs.get("category", "obvious"),
            text=kwargs.get("text", "日給5万円"),
        )

    def test_a_report_is_copied_with_its_wording(self):
        report = RiskReportSchema.model_validate(FIXTURES["danger"])

        outcome = evalset_metrics.outcome_from_report(self._case(), report)

        self.assertEqual(outcome.case_id, "o1")
        self.assertEqual(outcome.category, "obvious")
        self.assertEqual(outcome.level, Level.DANGER)
        self.assertEqual(outcome.label, "危険")
        self.assertEqual(outcome.score, 88)
        self.assertTrue(outcome.has_enough_info)
        self.assertIsNone(outcome.error)
        # 指標には出ないが、後から読み返すために持つ2つ
        self.assertEqual(outcome.summary, report.summary)
        self.assertEqual(outcome.advice, report.advice)

    def test_every_signal_is_searchable_in_one_string(self):
        """シグナルの名前と根拠が、照合できる形で1つにまとまること。"""
        report = RiskReportSchema.model_validate(FIXTURES["danger"])

        outcome = evalset_metrics.outcome_from_report(self._case(), report)

        for signal in report.signals:
            with self.subTest(signal=signal.name):
                self.assertIn(signal.name, outcome.signal_text)
                self.assertIn(signal.detail, outcome.signal_text)
