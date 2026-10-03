"""判定結果の見せ方の回帰テスト。

情報が足りないときに判定が付いたように読ませないこと、兆候が無い場合も
「安全」と言い切らないことを確認する。

あわせて、LLM の出力を画面に載せる前の関所（schema.py）が、画面の見え方を
崩す値（範囲外の危険度・空の文言・対応の無い配色）を通さないことを見る。
"""

from unittest import mock

import pydantic
from django.test import SimpleTestCase, TestCase, override_settings

from .. import service
from ..fixtures import FIXTURES
from ..schema import Level, MissingInfo, RiskReportSchema, Severity, Signal
from .helpers import JOB_TEXT


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


class MissingInfoIsSurfacedTests(TestCase):
    """判定材料が足りない場合の扱いの検証。

    貼り付けが部分的だと「事業者情報が無い」ように見えるため、それを危険の
    根拠にすると偽陽性になる。不足は危険度ではなく「貼り足しの案内」として
    画面に出す設計なので、その導線が生きているかを見る。
    """

    def test_listed_missing_info_forces_the_insufficient_flag(self):
        """不足項目が挙がっていれば、十分フラグは false 側に寄り、重複は畳まれること。"""
        report = RiskReportSchema.model_validate(
            _report(
                has_enough_info=True,
                missing_info=["事業者情報", "事業者情報", "仕事内容"],
            )
        )

        self.assertFalse(report.has_enough_info)
        self.assertEqual(
            [item["label"] for item in report.missing_info_hints],
            ["事業者情報", "仕事内容"],
        )

    @override_settings(VIEW_TEST_MODE=True)
    def test_result_page_asks_the_user_to_paste_the_missing_part(self):
        """情報不足の結果では、不足項目と貼り足しの案内が画面に出ること。"""
        # VIEW_TEST_MODE はランダムに1件選ぶため、情報不足のケースに固定する
        with mock.patch.object(
            service, "FIXTURES", {"insufficient": FIXTURES["insufficient"]}
        ):
            response = self.client.post("/", {"mode": "text", "text": JOB_TEXT})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "この文章だけでは、まだ判断しきれません")
        self.assertContains(response, "その部分も貼り付けて")
        self.assertContains(response, "事業者情報")
        self.assertContains(response, "応募・連絡方法")

    def test_safe_result_is_not_declared_safe(self):
        """兆候が無い場合も「安全」と言い切らないこと（偽陰性は取り返しがつかない）。"""
        report = RiskReportSchema.model_validate(_report(level="安全"))

        self.assertEqual(report.level_label, "危険な兆候なし")

    @override_settings(VIEW_TEST_MODE=True)
    def test_safe_result_page_shows_no_safe_verdict(self):
        """結果ページに判定として「安全」の文字を出さないこと。"""
        with mock.patch.object(service, "FIXTURES", {"safe": FIXTURES["safe"]}):
            response = self.client.post("/", {"mode": "text", "text": JOB_TEXT})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "危険な兆候なし")
        # fixture の文言も含め、「安全です」と読める断定が画面に出ていないこと
        self.assertNotContains(response, "安全です")

    def test_insufficient_result_is_not_labeled_as_a_verdict(self):
        """情報不足なら、判定名（安全など）も判定色も表示に使わないこと。"""
        report = RiskReportSchema.model_validate(
            _report(level="安全", has_enough_info=False, missing_info=["事業者情報"])
        )

        self.assertEqual(report.level_label, "情報不足")
        self.assertNotEqual(report.bs_color, "success")
        # 判定そのものは保持し、表示だけを差し替えている
        self.assertEqual(report.level, "安全")

    @override_settings(VIEW_TEST_MODE=True)
    def test_insufficient_result_page_shows_no_safe_badge(self):
        """情報不足の結果が「安全」の緑バッジで出ないこと。"""
        # level が安全寄りに付いた情報不足のケース（偽陽性の裏返しの見え方）
        fixture = {**FIXTURES["insufficient"], "level": "安全"}
        with mock.patch.object(service, "FIXTURES", {"insufficient": fixture}):
            response = self.client.post("/", {"mode": "text", "text": JOB_TEXT})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "情報不足")
        self.assertNotContains(response, "text-bg-success")

    @override_settings(VIEW_TEST_MODE=True)
    def test_sufficient_result_shows_no_notice(self):
        """情報がそろっている結果では、案内が出ないこと（常時表示になっていないか）。"""
        with mock.patch.object(service, "FIXTURES", {"danger": FIXTURES["danger"]}):
            response = self.client.post("/", {"mode": "text", "text": JOB_TEXT})

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "この文章だけでは、まだ判断しきれません")


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
