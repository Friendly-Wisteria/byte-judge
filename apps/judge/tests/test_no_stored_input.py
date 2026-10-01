"""入力内容がサーバー側に残らないことの回帰テスト。

設定値の目視確認では将来の変更で退行しても気づけないため、実際にリクエストを
通して外部に残る場所（ディスク / ログ / セッション / キャッシュ）を検査する。
"""

import logging
import os
import tempfile
from unittest import mock, skip

from django.conf import settings
from django.core.files import uploadedfile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings

from .. import forms, service, views
from .helpers import (
    IMAGE_PAUSED,
    JOB_TEXT,
    MARKER,
    api_status_error,
    capture_logs,
    png_at_least,
    schema_validation_error,
    upload,
)


class UploadNeverTouchesDiskTests(TestCase):
    """アップロードされたファイルがディスクに書き出されないことの検証。

    画像入力は停止中だが、multipart のパースはフォームより手前で走るため、
    ファイルを添えて POST する経路自体は生きている（ハンドラ次第では
    ディスクに書かれる）。約束は機能の停止とは独立に保つ必要があるので、
    下の2件は画像の再開を待たずに常時実行する。残りは画像フィールドが
    ある前提の検証なので、再開時まで眠らせる。
    """

    def test_only_the_memory_upload_handler_is_configured(self):
        # 一時ファイル書き出しハンドラが復活したら落ちる。ハンドラが増えると
        # ディスクに書く経路も戻り得るため、リストごと固定する。
        self.assertEqual(
            list(settings.FILE_UPLOAD_HANDLERS),
            ["django.core.files.uploadhandler.MemoryFileUploadHandler"],
        )

    def _spy_on_temporary_files(self, created):
        """TemporaryUploadedFile が作られたら、そのパスを created に記録する。"""
        original_init = uploadedfile.TemporaryUploadedFile.__init__

        def spy_init(temp_file, *args, **kwargs):
            original_init(temp_file, *args, **kwargs)
            created.append(temp_file.temporary_file_path())

        return spy_init

    @override_settings(VIEW_TEST_MODE=True)
    def test_an_uploaded_file_leaves_nothing_on_disk(self):
        """ファイルを添えて POST しても、一時ファイルが作られないこと。

        「処理後に消えている」だけでは、一度ディスクに書いてから消す実装でも
        通ってしまう。そもそも TemporaryUploadedFile が生成されないことも見る。

        大きさは2通り試す。Django 既定の 2.5MB 超（ハンドラと
        FILE_UPLOAD_MAX_MEMORY_SIZE を既定に戻すと書き出される大きさ）と、
        こちらの上限超（ハンドラだけ既定に戻すと書き出される大きさ）。
        中身は判定に渡らないので、PNG にせず乱数で大きさだけ作る。
        """
        sizes = {
            "Django 既定の 2.5MB 超": 2621440 + 1,
            "こちらの上限超": settings.FILE_UPLOAD_MAX_MEMORY_SIZE + 1,
        }
        for label, size in sizes.items():
            with self.subTest(size=label):
                created = []
                attached = SimpleUploadedFile(
                    "shot.png", os.urandom(size), content_type="image/png"
                )
                with tempfile.TemporaryDirectory() as temp_dir:
                    with (
                        override_settings(FILE_UPLOAD_TEMP_DIR=temp_dir),
                        mock.patch.object(
                            uploadedfile.TemporaryUploadedFile,
                            "__init__",
                            self._spy_on_temporary_files(created),
                        ),
                    ):
                        response = self.client.post(
                            "/", {"text": JOB_TEXT, "image": attached}
                        )

                    self.assertEqual(
                        sorted(os.listdir(temp_dir)),
                        [],
                        "リクエスト処理後も一時ディレクトリにファイルが残っている",
                    )

                # リクエストが途中で弾かれず、一時ファイルなしで form_valid の処理まで通っている
                # 判定処理に画像が渡らないことは test_image_input.py で見ている。
                self.assertContains(response, "危険度")
                self.assertEqual(
                    created,
                    [],
                    "TemporaryUploadedFile が生成された＝入力が一度ディスクに書かれている",
                )

    @skip(IMAGE_PAUSED)
    def test_memory_limit_stays_above_the_form_limit(self):
        # form の上限を下回ると、拒否対象のファイルが握り潰されて
        # 「画像サイズが大きすぎます」を返せなくなる
        self.assertGreater(settings.FILE_UPLOAD_MAX_MEMORY_SIZE, forms.MAX_IMAGE_SIZE)

    @skip(IMAGE_PAUSED)
    @override_settings(VIEW_TEST_MODE=True)
    def test_image_above_django_default_threshold_is_still_processed(self):
        """Django 既定の 2.5MB を超える画像が、メモリ上で判定まで通ること。

        ディスク書き出しを止めた副作用で通常サイズのスクショが弾かれていないかを見る。
        """
        seen = {}
        original_form_valid = views.IndexView.form_valid

        def capture(view, form):
            seen["type"] = type(form.cleaned_data.get("image")).__name__
            return original_form_valid(view, form)

        png = png_at_least(3 * 1024 * 1024)
        # Django 既定の 2.5MB 超（＝旧実装ならディスク行き）かつ form の上限内
        self.assertGreater(len(png), 2621440)
        self.assertLess(len(png), forms.MAX_IMAGE_SIZE)

        with mock.patch.object(views.IndexView, "form_valid", capture):
            response = self.client.post(
                "/", {"mode": "image", "text": "", "image": upload(png)}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen.get("type"), "InMemoryUploadedFile")
        self.assertContains(response, "危険度")


class JobTextKeepsTheMarkerDetectableTests(SimpleTestCase):
    """テストに使うJOB_TEXTの検出可能な位置にMARKERが配置されていることを検証。

    このテストがなければ、ログにユーザーの入力が含まれないテストが漏れていてもグリーンになる事故が起こる
    （test_schema_validation_error_does_not_log_the_offending_value）
    """

    def test_job_text_starts_with_marker(self):
        """テストの前提として、JOB_TEXTの冒頭が識別用のMARKERから始まること。

        変異テスト: JOB_TEXT内のMARKERの前に数文字たす。
        """
        self.assertTrue(JOB_TEXT.startswith(MARKER))

    def test_job_text_marker_survives_truncation(self):
        """テストの前提として、デフォルトのValidationErrorの文字列表現で
        文字数を丸める形式でもマーカー全体が露出すること。

        変異テスト: MARKERの文字数を25文字以上にする / JOB_TEXT内のMARKERを末尾に移し、さらにその後ろに20文字たす
        """
        e = schema_validation_error()
        self.assertIn(
            MARKER, str(e), "str(e)で、文字数の丸めによってマーカーが消えてしまっている"
        )


@override_settings(VIEW_TEST_MODE=False)
class LogsNeverContainInputTests(TestCase):
    """判定が失敗したときのログに、求人テキストが出ないことの検証。"""

    def _post_with_api_failure(self, error):
        """Claude API 呼び出しが error で失敗する状況で POST する。"""
        with mock.patch.object(service.anthropic, "Anthropic") as client_class:
            client_class.return_value.messages.parse.side_effect = error
            with capture_logs() as logs:
                response = self.client.post("/", {"mode": "text", "text": JOB_TEXT})
        # 判定は表示されず、ユーザーにはエラーが出る（文言は別テストで検証）
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "alert-danger")
        self.assertIsNone(response.context.get("result"))
        return logs.text

    def test_api_status_error_does_not_log_the_request_body(self):
        output = self._post_with_api_failure(api_status_error())
        # ログ自体は出ている（握り潰して通ったのではないことの確認）
        self.assertIn("Claude API error", output)
        self.assertNotIn(MARKER, output)

    def test_schema_validation_error_does_not_log_the_offending_value(self):
        """不適合だった値（求人文を引用しうる）が input_value として出ないこと。

        pydantic の ValidationError は文字列表現に input_value を含むため、
        exc_info でトレースを出すと求人文が漏れる。
        """
        output = self._post_with_api_failure(schema_validation_error())
        # どの項目がどの理由で落ちたかは残っている
        self.assertIn("Response did not satisfy RiskReportSchema", output)
        self.assertIn("enum", output)
        self.assertNotIn(MARKER, output)

    def test_unexpected_exception_does_not_log_local_variables(self):
        """想定外の例外のトレースに、ローカル変数の求人テキストが出ないこと。"""
        output = self._post_with_api_failure(RuntimeError("boom"))
        self.assertIn("Unexpected error", output)
        self.assertIn("RuntimeError", output)
        self.assertNotIn(MARKER, output)

    def test_anthropic_sdk_logger_cannot_emit_debug_payloads(self):
        """ANTHROPIC_LOG=debug で SDK が送信ペイロード全文を出す経路を塞げていること。"""
        # import 時点で適用済みであること
        self.assertGreaterEqual(
            logging.getLogger("anthropic").getEffectiveLevel(), logging.INFO
        )

        # ANTHROPIC_LOG=debug が設定された状態（SDK が import 時に行う設定）を
        # 再現し、ガードがそれを引き上げ直すことを確認する
        sdk_logger = logging.getLogger("anthropic")
        original_level = sdk_logger.level
        try:
            sdk_logger.setLevel(logging.DEBUG)
            service._silence_sdk_payload_logging()
            self.assertGreaterEqual(sdk_logger.getEffectiveLevel(), logging.INFO)
        finally:
            sdk_logger.setLevel(original_level)

    def test_view_marks_post_parameters_as_sensitive(self):
        """エラーレポートで POST の求人テキストがマスクされること。"""
        from django.views.debug import SafeExceptionReporterFilter

        with mock.patch.object(service.anthropic, "Anthropic") as client_class:
            client_class.return_value.messages.parse.side_effect = RuntimeError("boom")
            response = self.client.post("/", {"mode": "text", "text": JOB_TEXT})

        request = response.wsgi_request
        cleansed = SafeExceptionReporterFilter().get_post_parameters(request)
        self.assertNotIn(MARKER, str(dict(cleansed)))


@override_settings(VIEW_TEST_MODE=True)
class SessionNeverContainsInputTests(TestCase):
    """判定後のセッションに求人テキストが残らないことの検証。

    現状のフローは POST → そのままレンダリングでリダイレクトを挟まないため、
    判定結果をセッションで受け渡す必要がない。将来リダイレクト方式に変えて
    セッション経由にした場合に、ここで気づけるようにしておく。
    """

    def test_session_and_cookies_are_free_of_the_job_text(self):
        response = self.client.post("/", {"mode": "text", "text": JOB_TEXT})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "危険度")

        # セッションの中身
        self.assertNotIn(MARKER, str(dict(self.client.session.items())))

        # クッキー（messages は既定で CookieStorage に載る）
        for cookie in response.cookies.values():
            self.assertNotIn(MARKER, cookie.value)

        # 画面に出すメッセージ
        for message in response.context["messages"]:
            self.assertNotIn(MARKER, str(message))

    def test_no_session_row_is_created(self):
        """そもそもセッションが作られないこと（＝DB に何も書かれないこと）。"""
        from django.contrib.sessions.models import Session

        self.client.post("/", {"mode": "text", "text": JOB_TEXT})

        self.assertEqual(Session.objects.count(), 0, "セッション行が作成されている")
        self.assertNotIn("sessionid", self.client.cookies)


class ResultPageIsNotCachedTests(TestCase):
    """判定結果のページがキャッシュに保存されない指定になっていることの検証。"""

    @override_settings(VIEW_TEST_MODE=True)
    def test_result_response_forbids_storing(self):
        response = self.client.post("/", {"mode": "text", "text": JOB_TEXT})

        self.assertEqual(response.status_code, 200)
        cache_control = response.headers.get("Cache-Control", "")
        # no-store が外れると、判定結果がブラウザのディスクキャッシュに残りうる
        self.assertIn("no-store", cache_control)
        self.assertIn("private", cache_control)

    def test_form_page_forbids_storing(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))
