"""判定 JSON の値域と、配色の対応の回帰テスト。

schema.py は、LLM の出力を画面に載せる前の関所。範囲外の score や空の要約を
通すと、そのまま利用者の画面に出る。ここでは受け付けない値を確かめる。
あわせて、配線テストモードで使う見本が全件この関所を通ることも見る。
"""

import pydantic
from django.test import SimpleTestCase

from ..fixtures import FIXTURES
from ..schema import Level, MissingInfo, RiskReportSchema, Severity, Signal


def _report(**overrides):
    """妥当な判定 JSON に、確かめたい値だけを上書きして作る。"""
    base = {
        "score": 50,
        "level": "要注意",
        "summary": "要約",
        "signals": [],
        "advice": "助言",
        "has_enough_info": True,
    }
    return {**base, **overrides}


class ScoreStaysInRangeTests(SimpleTestCase):
    """危険度の値域の検証。

    score は画面のバーの長さになる。範囲外を通すと、バーがはみ出したり
    消えたりして、危険度の見え方が実際とずれる。
    """

    def test_both_ends_of_the_range_are_accepted(self):
        for score in (0, 100):
            with self.subTest(score=score):
                report = RiskReportSchema.model_validate(_report(score=score))
                self.assertEqual(report.score, score)

    def test_a_score_outside_the_range_is_refused(self):
        for score in (-1, 101):
            with (
                self.subTest(score=score),
                self.assertRaises(pydantic.ValidationError),
            ):
                RiskReportSchema.model_validate(_report(score=score))


class EmptyWordingIsRefusedTests(SimpleTestCase):
    """要約と助言が空でないことの検証。

    どちらも画面の主役で、空のまま出すと「判定は出ているのに何も書かれて
    いない」画面になる。判定を返せなかったときの案内とも見分けがつかない。

    空白だけの文字列も同じ見え方になるため、前後の空白を取り除いてから
    長さを見ている（schema.NonBlankText）。
    """

    def test_an_empty_summary_is_refused(self):
        with self.assertRaises(pydantic.ValidationError):
            RiskReportSchema.model_validate(_report(summary=""))

    def test_an_empty_advice_is_refused(self):
        with self.assertRaises(pydantic.ValidationError):
            RiskReportSchema.model_validate(_report(advice=""))

    def test_whitespace_alone_is_refused(self):
        """空白や改行だけの文字列は、画面では空と変わらないこと。"""
        for field in ("summary", "advice"):
            for value in (" ", "\n\n", "　"):  # 半角・改行・全角
                with (
                    self.subTest(field=field, value=repr(value)),
                    self.assertRaises(pydantic.ValidationError),
                ):
                    RiskReportSchema.model_validate(_report(**{field: value}))

    def test_surrounding_whitespace_is_trimmed(self):
        """通る値からは、前後の空白が取り除かれること。"""
        report = RiskReportSchema.model_validate(
            _report(summary="  要約です\n", advice="\t助言です  ")
        )

        self.assertEqual(report.summary, "要約です")
        self.assertEqual(report.advice, "助言です")


class ColoursMatchTheSeverityTests(SimpleTestCase):
    """深刻度・判定ラベルと配色の対応の検証。

    配色は「どれくらい危ないか」を最初に伝える部分。対応がずれると、
    深刻なシグナルが穏やかな色で出る。
    """

    def test_each_severity_keeps_its_colour(self):
        expected = {
            Severity.HIGH: "danger",
            Severity.MID: "warning",
            Severity.LOW: "secondary",
        }
        for severity, colour in expected.items():
            with self.subTest(severity=severity.value):
                signal = Signal(name="n", severity=severity, detail="d")
                self.assertEqual(signal.bs_color, colour)

    def test_no_severity_is_left_without_a_colour(self):
        """深刻度が増えたとき、対応表への追加漏れをここで気づけるようにする。"""
        for severity in Severity:
            with self.subTest(severity=severity.value):
                signal = Signal(name="n", severity=severity, detail="d")
                self.assertTrue(signal.bs_color)

    def test_each_level_keeps_its_colour(self):
        expected = {
            Level.DANGER: "danger",
            Level.CAUTION: "warning",
            Level.SAFE: "success",
        }
        for level, colour in expected.items():
            with self.subTest(level=level.value):
                report = RiskReportSchema.model_validate(_report(level=level.value))
                self.assertEqual(report.bs_color, colour)

    def test_no_missing_info_item_is_left_without_a_hint(self):
        """不足項目が増えたとき、貼り足しの案内の追加漏れに気づけるようにする。"""
        report = RiskReportSchema.model_validate(
            _report(missing_info=[item.value for item in MissingInfo])
        )

        self.assertEqual(len(report.missing_info_hints), len(MissingInfo))


class EveryFixtureCanBeDisplayedTests(SimpleTestCase):
    """配線テストモードの見本が、全件そのまま画面に出せることの検証。

    VIEW_TEST_MODE は FIXTURES からランダムに1件選ぶ。1件だけ壊れていても、
    その1件が選ばれたときにしか分からず、再現もしにくい。全件をここで通す。
    """

    def test_every_fixture_passes_the_schema(self):
        for name, data in FIXTURES.items():
            with self.subTest(fixture=name):
                RiskReportSchema.model_validate(data)

    def test_every_fixture_has_something_to_show(self):
        """画面に出す3点（ラベル・配色・要約）が埋まっていること。"""
        for name, data in FIXTURES.items():
            with self.subTest(fixture=name):
                report = RiskReportSchema.model_validate(data)
                self.assertTrue(report.level_label)
                self.assertTrue(report.bs_color)
                self.assertTrue(report.summary)

    def test_the_fixtures_cover_every_way_a_result_can_look(self):
        """見本が、画面の見え方を全通り持っていること。

        配線テストは色とラベルの目視確認が目的なので、どれかの見え方の
        見本が無いと、その経路だけ誰の目にも触れないまま公開される。
        """
        labels = {
            RiskReportSchema.model_validate(data).level_label
            for data in FIXTURES.values()
        }

        self.assertEqual(labels, {"危険", "要注意", "危険な兆候なし", "情報不足"})
