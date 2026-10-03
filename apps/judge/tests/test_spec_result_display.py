"""判定結果の見せ方の回帰テスト。

情報が足りないときに判定が付いたように読ませないこと、兆候が無い場合も
「安全」と言い切らないことを確認する。

あわせて、LLM の出力を画面に載せる前の関所（schema.py）が、画面の見え方を
崩す値（範囲外の危険度・空の文言・対応の無い配色）を通さないことを見る。
"""

from unittest import mock

import pydantic
from django.test import SimpleTestCase, override_settings

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


class MissingInfoIsSurfacedTests(SimpleTestCase):
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

    def test_a_signal_without_a_severity_is_refused(self):
        """深刻度の無い兆候は通さないこと。

        配色は `_SEVERITY_COLOR` を引いて決めるため、既定値を与えると
        引けない色が出る。必須のまま保つ。
        """
        with self.assertRaises(pydantic.ValidationError):
            RiskReportSchema.model_validate(
                _report(
                    signals=[
                        {
                            "name": "既存の手口",
                            "detail": "報道されている手口に似ています",
                        }
                    ]
                )
            )

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


class PartialSignalWordingIsStillShownTests(SimpleTestCase):
    """兆候の名前や根拠が空でも、判定ごと捨てないことの検証。

    要約と助言は空を止めるが（上のクラス）、兆候は止めない。片方が空でも
    もう一方が読めれば利用者は何かを持ち帰れるうえ、空の兆候が1件混ざった
    だけでまともな兆候まで見えなくなるほうが損失が大きい。意図した非対称
    なので、`NonBlankText` を足されたら落ちるようにしておく（#77）。
    """

    def test_a_signal_with_a_blank_half_is_kept(self):
        """名前だけ・根拠だけの兆候も、落とさずに残すこと。
        変異テスト: schema.Signal の name / detail を NonBlankText にする
        """
        report = RiskReportSchema.model_validate(
            _report(
                signals=[
                    {
                        "name": "",
                        "severity": Severity.HIGH,
                        "detail": "報道されている手口に似ています",
                    },
                    {
                        "name": "相場を大幅に超えた報酬",
                        "severity": Severity.MID,
                        "detail": "",
                    },
                ]
            )
        )

        self.assertEqual(len(report.signals), 2)
        self.assertEqual(report.signals[0].detail, "報道されている手口に似ています")
        self.assertEqual(report.signals[1].name, "相場を大幅に超えた報酬")
        # 読めるほうが残っていれば、配色も引ける
        self.assertTrue(all(signal.bs_color for signal in report.signals))


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


class MissingInfoHintsMatchTheItemsTests(SimpleTestCase):
    """不足項目と、貼り足しの案内の対応の検証。

    情報不足の結果で、このアプリがいちばん価値を出すのは「どこを貼り足せば
    判定できるか」を伝える導線。案内が欠けていたり、別の項目のものが出ると、
    利用者は言われたとおりに貼り足しても不足が埋まらず、2回目も情報不足になる。
    """

    def _hints(self):
        """全項目を不足として挙げたときの、ラベル→案内の対応を返す。"""
        report = RiskReportSchema.model_validate(
            _report(missing_info=[item.value for item in MissingInfo])
        )
        return report.missing_info_hints

    def test_no_missing_info_item_is_left_without_a_hint(self):
        """MissingInfoと案内の対応表に、数の上で過不足がないこと

        項目を増やしたときの、対応表への追加漏れに気づけるようにする。
        気づかせているのは schema._MISSING_HINT[item] の KeyError で、下の
        件数の比較ではない（内包表記は入力1件につき必ず1件返すため）。件数の
        比較のほうは「挙がった項目を黙って捨てないこと」の検査にあたる。
        変異テスト: MissingInfo に項目を足して _MISSING_HINT には足さない /
        _reconcile_missing_info に、対応の無い項目を捨てる処理を入れる
        """
        self.assertEqual(len(self._hints()), len(MissingInfo))

    def test_every_hint_tells_the_user_what_to_paste(self):
        """不足している項目のラベルと、具体的に何を足せばいいかの説明が明記されること。

        どちらかが空だと片方だけの行になり、「貼り足してください」と言いながら
        何を足せばよいかが利用者に渡らない。
        変異テスト: _MISSING_HINT のどれかの文言を "" にする /
        MissingInfo のどれかの値を "" にする
        """
        for hint in self._hints():
            with self.subTest(label=hint["label"]):
                self.assertTrue(hint["label"].strip())
                self.assertTrue(hint["hint"].strip())

    def test_each_hint_points_at_its_own_item(self):
        """ラベルと案内の対応がずれていないこと。

        対応表のキーと文言がコピペでずれても、件数も KeyError も変わらないため
        上の2件では気づけない。文言そのものは書き写さず（二重管理になる）、
        ずれたら必ず壊れる語だけを項目ごとに1つ見る。
        変異テスト: _MISSING_HINT の値を、隣の項目のものと入れ替える
        """
        keyword = {
            MissingInfo.OPERATOR: "募集元",
            MissingInfo.JOB_DETAIL: "仕事",
            MissingInfo.PAY: "時給",
            MissingInfo.WORKPLACE: "勤務地",
            MissingInfo.CONTACT: "連絡先",
        }
        hints = {hint["label"]: hint["hint"] for hint in self._hints()}

        for item in MissingInfo:
            with self.subTest(item=item.value):
                # 項目を増やしたときは、ここも KeyError で気づく
                self.assertIn(keyword[item], hints[item.value])
