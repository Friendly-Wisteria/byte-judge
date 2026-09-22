"""判定結果の見せ方の回帰テスト。

情報が足りないときに判定が付いたように読ませないこと、兆候が無い場合も
「安全」と言い切らないことを確認する。
"""

from unittest import mock

from django.test import TestCase, override_settings

from .. import service
from ..fixtures import FIXTURES
from ..schema import RiskReportSchema
from .helpers import JOB_TEXT


class MissingInfoIsSurfacedTests(TestCase):
    """判定材料が足りない場合の扱いの検証。

    貼り付けが部分的だと「事業者情報が無い」ように見えるため、それを危険の
    根拠にすると偽陽性になる。不足は危険度ではなく「貼り足しの案内」として
    画面に出す設計なので、その導線が生きているかを見る。
    """

    def test_listed_missing_info_forces_the_insufficient_flag(self):
        """不足項目が挙がっていれば、十分フラグは false 側に寄り、重複は畳まれること。"""
        report = RiskReportSchema.model_validate(
            {
                "score": 20,
                "level": "要注意",
                "summary": "s",
                "signals": [],
                "advice": "a",
                # 不足を挙げながら「十分」と返ってくる矛盾したケース
                "has_enough_info": True,
                "missing_info": ["事業者情報", "事業者情報", "仕事内容"],
            }
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
        report = RiskReportSchema.model_validate(
            {
                "score": 12,
                "level": "安全",
                "summary": "s",
                "signals": [],
                "advice": "a",
                "has_enough_info": True,
            }
        )

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
            {
                "score": 12,
                "level": "安全",
                "summary": "s",
                "signals": [],
                "advice": "a",
                "has_enough_info": False,
                "missing_info": ["事業者情報"],
            }
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
