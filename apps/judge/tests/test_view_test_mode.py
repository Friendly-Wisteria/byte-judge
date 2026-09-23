"""配線テストモード（VIEW_TEST_MODE）の警告表示の回帰テスト。

このモードでは判定を LLM に投げず、fixtures から1件を選んで画面に出す。
見た目は本物の判定と区別がつかないため、「これは判定結果ではない」と告げる
警告バナーだけが誤認を防いでいる。バナーが消えても結果表示自体は成立して
しまうので、入口（GET）と結果表示（POST）の両方で出ることを確かめる。
"""

from unittest import mock

from django.test import TestCase, override_settings

from .. import views
from ..fixtures import FIXTURES
from ..schema import RiskReportSchema
from .helpers import JOB_TEXT


@override_settings(VIEW_TEST_MODE=True)
class WiringTestModeIsAnnouncedTests(TestCase):
    def test_the_warning_tells_the_user_not_to_use_the_result(self):
        """文言が「使わないで」と言い切っていること（存在するだけでは足りない）。"""
        self.assertIn("使用しないでください", views.VIEW_TEST_MODE_WARNING)

    def test_the_input_page_warns_before_anything_is_judged(self):
        self.assertContains(self.client.get("/"), views.VIEW_TEST_MODE_WARNING)

    def test_the_result_page_warns_next_to_the_sample_judgment(self):
        """結果が出ている画面にこそバナーが要る（ここが消えると誤認につながる）。"""
        response = self.client.post("/", {"mode": "text", "text": JOB_TEXT})

        self.assertIsNotNone(response.context.get("result"))
        self.assertContains(response, views.VIEW_TEST_MODE_WARNING)

    def test_the_warning_is_styled_as_a_warning(self):
        """注意の配色で出ること（本文に紛れると気づかれない）。"""
        self.assertContains(self.client.get("/"), 'class="alert alert-warning"')


@override_settings(VIEW_TEST_MODE=False)
class RealJudgmentIsNotLabelledAsASampleTests(TestCase):
    """本番の判定にバナーが出ないことの検証。

    出しっぱなしだと本物の判定まで「使用しないでください」と言うことになり、
    警告そのものが読み飛ばされるようになる。
    """

    def test_the_input_page_has_no_warning(self):
        self.assertNotContains(self.client.get("/"), views.VIEW_TEST_MODE_WARNING)

    def test_the_result_page_has_no_warning(self):
        report = RiskReportSchema.model_validate(FIXTURES["danger"])
        with mock.patch.object(views, "job_offer_risk_assess", return_value=report):
            response = self.client.post("/", {"mode": "text", "text": JOB_TEXT})

        self.assertIsNotNone(response.context.get("result"))
        self.assertNotContains(response, views.VIEW_TEST_MODE_WARNING)
