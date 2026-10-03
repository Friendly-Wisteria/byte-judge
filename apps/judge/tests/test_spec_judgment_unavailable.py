"""判定を返せないときの扱いの回帰テスト。

理由の振り分け（サービス層）と、画面の案内（相談先 #9110・188）の両方を見る。
判定が止まっていても、相談先の情報だけは届ける必要がある。
"""

import pathlib
import tempfile
from unittest import mock

import anthropic
from django.db import OperationalError
from django.test import SimpleTestCase, TestCase, override_settings

from .. import forms, service, views
from ..fixtures import FIXTURES
from ..models import DailyUsage
from ..schema import RiskReportSchema
from .helpers import (
    FORBIDDEN_TECHNICAL_TOKENS,
    JOB_TEXT,
    FakeRequest,
    FakeResponse,
    api_status_error,
    assert_consultation_is_offered,
    billing_error,
    rate_limit_error,
    schema_validation_error,
)


@override_settings(VIEW_TEST_MODE=False)
class AssessmentFailuresAreClassifiedTests(TestCase):
    """判定を返せない場合の、理由の振り分けの検証。

    UNAVAILABLE と FAILED で画面の案内が変わる（前者は「サービス側の問題なので
    文章を直しても解決しない」、後者は「もう一度お試しください」）。振り分けを
    間違えると、直しても解消しない失敗に再試行を促すことになる。API 由来の
    経路は ApiUnavailableIsGuidedToConsultationTests で見ているので、ここは
    そこに載っていない経路を埋める。
    """

    def _parse(self):
        """Claude API クライアントを差し替え、messages.parse のモックを返す。"""
        patcher = mock.patch.object(service.anthropic, "Anthropic")
        client_class = patcher.start()
        self.addCleanup(patcher.stop)
        return client_class.return_value.messages.parse

    def test_a_non_text_input_is_rejected_without_calling_the_api(self):
        """テキストでも画像でもない入力は、API に投げずに落とすこと。"""
        parse = self._parse()

        for value in (123, None, b"bytes", ["text"]):
            with self.subTest(value=type(value).__name__):
                self.assertIs(
                    service.job_offer_risk_assess(value),
                    service.AssessmentError.FAILED,
                )
        parse.assert_not_called()

    def test_a_missing_prompt_file_is_reported_as_failed(self):
        """判定プロンプトが読めないときは、API に投げずに落とすこと。

        配置・設定の誤りなので、投げても費用だけが出る。
        """
        parse = self._parse()
        missing = pathlib.Path(tempfile.mkdtemp()) / "nope.md"

        with mock.patch.object(service, "PROMPT_PATH", missing):
            result = service.job_offer_risk_assess(JOB_TEXT)

        self.assertIs(result, service.AssessmentError.FAILED)
        parse.assert_not_called()

    def test_an_unknown_model_is_reported_as_unavailable(self):
        """モデルIDの誤り（404）も、利用者から見れば「判定を受けられない」。"""
        parse = self._parse()
        parse.side_effect = anthropic.NotFoundError(
            "model not found",
            response=FakeResponse(404),
            body={"error": {"type": "not_found_error", "message": "model not found"}},
        )

        self.assertIs(
            service.job_offer_risk_assess(JOB_TEXT),
            service.AssessmentError.UNAVAILABLE,
        )

    def test_a_truncated_response_is_reported_as_failed(self):
        """max_tokens で切れた応答は、中身が取れても表示しないこと。"""
        parse = self._parse()
        # パースできる出力が付いていても、打ち切りの検出が優先されること
        parse.return_value = mock.Mock(
            stop_reason="max_tokens",
            parsed_output=RiskReportSchema.model_validate(FIXTURES["danger"]),
        )

        self.assertIs(
            service.job_offer_risk_assess(JOB_TEXT), service.AssessmentError.FAILED
        )

    def test_an_unparsable_response_is_reported_as_failed(self):
        """構造化出力を取り出せなかった場合も、結果を表示しないこと。"""
        parse = self._parse()
        parse.return_value = mock.Mock(stop_reason="end_turn", parsed_output=None)

        self.assertIs(
            service.job_offer_risk_assess(JOB_TEXT), service.AssessmentError.FAILED
        )


@override_settings(VIEW_TEST_MODE=False)
class ApiUnavailableIsGuidedToConsultationTests(TestCase):
    """LLM の判定を受けられないときの案内の検証。

    月額の利用上限・レート制限・API 障害・安全機構による拒否では、時間をおいても
    判定できるとは限らない。「失敗したので再試行を」で終わらせると判断がつかない
    まま放置されるため、相談先（#9110・188）まで案内できているかを見る。
    """

    def _assess_with_api_failure(self, error):
        with mock.patch.object(service.anthropic, "Anthropic") as client_class:
            client_class.return_value.messages.parse.side_effect = error
            return service.job_offer_risk_assess(JOB_TEXT)

    def test_rate_limit_is_reported_as_unavailable(self):
        self.assertIs(
            self._assess_with_api_failure(rate_limit_error()),
            service.AssessmentError.UNAVAILABLE,
        )

    def test_spend_limit_is_reported_as_unavailable(self):
        """月額の利用上限で止められた場合も同じ扱いになること。"""
        self.assertIs(
            self._assess_with_api_failure(billing_error()),
            service.AssessmentError.UNAVAILABLE,
        )

    def test_api_outage_is_reported_as_unavailable(self):
        for error in (
            api_status_error(),
            anthropic.APIConnectionError(request=FakeRequest()),
        ):
            with self.subTest(error=type(error).__name__):
                self.assertIs(
                    self._assess_with_api_failure(error),
                    service.AssessmentError.UNAVAILABLE,
                )

    def test_refusal_is_reported_as_unavailable(self):
        """安全機構が発火した場合（HTTP は 200）も判定は得られないこと。"""
        with mock.patch.object(service.anthropic, "Anthropic") as client_class:
            client_class.return_value.messages.parse.return_value = mock.Mock(
                stop_reason="refusal", stop_details=mock.Mock(category="cyber")
            )
            result = service.job_offer_risk_assess(JOB_TEXT)

        self.assertIs(result, service.AssessmentError.UNAVAILABLE)

    def test_schema_failure_is_not_reported_as_unavailable(self):
        """応答自体は得られている失敗は、従来どおり再試行の案内に倒すこと。"""
        self.assertIs(
            self._assess_with_api_failure(schema_validation_error()),
            service.AssessmentError.FAILED,
        )

    def test_page_tells_the_user_it_is_unavailable_and_where_to_ask(self):
        with mock.patch.object(
            views,
            "job_offer_risk_assess",
            return_value=service.AssessmentError.UNAVAILABLE,
        ):
            response = self.client.post("/", {"mode": "text", "text": JOB_TEXT})

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context.get("result"))
        self.assertContains(response, "いまは、AIによる判定を行えません")
        assert_consultation_is_offered(self, response)

    def test_parse_failure_also_offers_the_hotlines(self):
        """応答を受け取れなかった場合も、再試行の案内だけで終わらせないこと。"""
        with mock.patch.object(
            views, "job_offer_risk_assess", return_value=service.AssessmentError.FAILED
        ):
            response = self.client.post("/", {"mode": "text", "text": JOB_TEXT})

        self.assertContains(response, "判定の結果を、正しく受け取れませんでした")
        assert_consultation_is_offered(self, response)


@override_settings(VIEW_TEST_MODE=False)
class DatabaseFailureIsGuidedToConsultationTests(TestCase):
    """データベースに触れないときの案内の検証。

    サイト全体の枠は DB で数えているため、接続できないと判定の POST 経路で
    例外が上へ抜け、本番では Django の素の 500 ページが返っていた（#45）。
    LLM 側の失敗には相談先を出しているのに、DB 側の失敗だけがその網から
    外れていた。不安な状態で訪れた人に、案内の無いページを返すことになる。

    接続は get_or_create の後でも切れる（保存期間の削除・確保の UPDATE）ため、
    そちらで落ちる場合も見る。
    """

    def _post_with_database_failure(self, attribute):
        """DailyUsage への問い合わせを、接続断に差し替えて POST する。"""
        error = OperationalError("could not connect to server")
        with mock.patch.object(DailyUsage.objects, attribute, side_effect=error):
            return self.client.post("/", {"mode": "text", "text": JOB_TEXT})

    def test_a_connection_failure_is_guided_instead_of_a_bare_500(self):
        for attribute in ("get_or_create", "filter"):
            with self.subTest(attribute=attribute):
                response = self._post_with_database_failure(attribute)

                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.context.get("result"))
                self.assertContains(response, "いまは、判定を行えません")
                assert_consultation_is_offered(self, response)

    def test_the_daily_limit_message_is_not_shown(self):
        """上限で埋まったときの案内と混同しないこと。

        「日付が変わると、また使えるようになります」は、DB 障害では事実と
        違う（明日また来ればよい／いま一時的に動いていない、の違い）。
        """
        response = self._post_with_database_failure("get_or_create")

        self.assertNotContains(response, "本日ぶんの判定枠")

    def test_the_api_is_not_called_when_the_slot_cannot_be_reserved(self):
        """枠を数えられない状態では、費用の出る判定に進まないこと（fail closed）。"""
        with mock.patch.object(views, "job_offer_risk_assess") as assess:
            self._post_with_database_failure("get_or_create")

        assess.assert_not_called()


class UnavailableGuidanceTests(SimpleTestCase):
    """判定を返せないときの案内そのものの検証。

    上限・API 障害・拒否・パース失敗のどの経路でも、判定は止まっていても
    相談先の情報は届ける必要がある。ページ下部の注意書きにも同じ番号が
    あるため、レンダリング結果ではなく案内の文言を直接検査する。
    """

    def _messages(self):
        return {
            "個人の日次上限": views.daily_quota_error(),
            "サイト全体の日次上限": views.SITE_QUOTA_ERROR,
            "サイト側の障害で判定不可": views.SITE_UNAVAILABLE_ERROR,
            "API 側の事情で判定不可": views.LLM_UNAVAILABLE_ERROR,
            "判定結果を受け取れず": views.ASSESSMENT_FAILED_ERROR,
        }

    def test_every_message_offers_both_hotlines(self):
        for label, text in self._messages().items():
            with self.subTest(case=label):
                self.assertIn("#9110", text)
                self.assertIn("188", text)

    def test_no_message_leaks_a_technical_detail(self):
        """技術的なエラーコードや内部の名前を利用者に見せないこと。"""
        for label, text in self._messages().items():
            for token in FORBIDDEN_TECHNICAL_TOKENS:
                with self.subTest(case=label, token=token):
                    self.assertNotIn(token, text)

    def test_quota_messages_say_when_judging_resumes(self):
        """上限で断る場合は、いつ使えるようになるかを伝えること。"""
        for label in ("個人の日次上限", "サイト全体の日次上限"):
            with self.subTest(case=label):
                self.assertIn("0時", self._messages()[label])


@override_settings(VIEW_TEST_MODE=True)
class InputErrorIsNotUnavailableTests(TestCase):
    """入力を直せば通るエラーを、判定不可の案内と混同しないことの検証。

    空入力に相談先まで出すと、案内が薄まって本当に判定を受けられないときに
    効かなくなる。見出しと相談先は、判定不可のときだけ出す。
    """

    def test_empty_input_is_not_dressed_as_unavailable(self):
        response = self.client.post("/", {"mode": "text", "text": ""})

        self.assertContains(response, forms.NO_INPUT_ERROR)
        self.assertNotContains(response, "判定をお届けできませんでした")
        self.assertNotContains(response, "警察相談専用ダイヤル #9110")
