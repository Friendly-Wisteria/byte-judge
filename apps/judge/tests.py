"""「入力内容をサーバー側に残さない」ことの回帰テスト。

設定値の目視確認では将来の変更で退行しても気づけないため、
実際にリクエストを通して外部に残る場所（ディスク / ログ / セッション）を検査する。
"""

import io
import logging
import math
import os
import tempfile
from unittest import mock

import anthropic
import pydantic
from django.conf import settings
from django.core.files import uploadedfile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image

from . import forms, service, views
from .schema import RiskReportSchema

# 求人テキストに紛れ込ませる目印。ディスク・ログ・セッションのいずれにも
# 現れてはいけない。fixtures の文言と偶然一致しないよう一意な文字列にする。
MARKER = "ZZMARKER7f3a9cZZ"
JOB_TEXT = f"日給5万円・即日手渡し・Telegramで連絡ください 合言葉:{MARKER}"


def png_at_least(min_bytes):
    """指定バイト数以上の PNG を作る。

    ランダムノイズを使い、PNG の可逆圧縮が効かないようにしてサイズを稼ぐ。
    非圧縮なら 1px = 3バイトなので、そこから一辺を逆算し、足りなければ少しずつ広げる。
    """
    side = math.ceil(math.sqrt(min_bytes / 3))
    while True:
        image = Image.frombytes("RGB", (side, side), os.urandom(side * side * 3))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        data = buffer.getvalue()
        if len(data) >= min_bytes:
            return data
        side = int(side * 1.05) + 1


def upload(png_bytes):
    return SimpleUploadedFile("shot.png", png_bytes, content_type="image/png")


class capture_logs:
    """アプリと SDK の全ロガーの出力を、例外トレースも含めて集める。

    apps.judge は propagate=False なので root に流れてこない。取りこぼしを
    防ぐため、関係するロガーそれぞれにハンドラを付け、レベルも DEBUG まで
    下げる（＝漏れるものがあれば必ず捕まる状態にして検査する）。
    """

    LOGGER_NAMES = ("", "apps.judge", "anthropic", "httpx", "django")

    def __enter__(self):
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setLevel(logging.DEBUG)
        self.handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        self._saved = []
        for name in self.LOGGER_NAMES:
            target = logging.getLogger(name)
            self._saved.append((target, target.level))
            target.addHandler(self.handler)
            target.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc_info):
        for target, level in self._saved:
            target.removeHandler(self.handler)
            target.setLevel(level)
        return False

    @property
    def text(self):
        return self.stream.getvalue()


class FakeRequest:
    """SDK 例外が保持する httpx.Request の代役。

    本物は送信ボディ（求人テキスト・画像の base64）を保持している。
    repr と content の両方に目印を仕込み、うっかりログに出れば検知できるようにする。
    """

    content = JOB_TEXT.encode("utf-8")

    def __repr__(self):
        return f"<Request POST /v1/messages body={JOB_TEXT}>"


class FakeResponse:
    status_code = 400
    headers = {"request-id": "req_test"}
    request = FakeRequest()


def api_status_error():
    return anthropic.APIStatusError(
        "Invalid request",
        response=FakeResponse(),
        body={"error": {"type": "invalid_request_error", "message": "Invalid request"}},
    )


def schema_validation_error():
    """求人文が LLM 応答経由で混入した状態のスキーマ不適合を再現する。"""
    try:
        RiskReportSchema.model_validate(
            {
                "score": 5,
                "level": JOB_TEXT,  # 不正な列挙値 → input_value として保持される
                "summary": "x",
                "signals": [],
                "advice": "y",
            }
        )
    except pydantic.ValidationError as e:
        return e
    raise AssertionError("ValidationError を再現できていない")


class UploadNeverTouchesDiskTests(TestCase):
    """アップロード画像がディスクに書き出されないことの検証。"""

    def test_settings_exclude_the_temporary_file_handler(self):
        # 一時ファイル書き出しハンドラが復活したら落ちる
        self.assertNotIn(
            "django.core.files.uploadhandler.TemporaryFileUploadHandler",
            settings.FILE_UPLOAD_HANDLERS,
        )

    def test_memory_limit_stays_above_the_form_limit(self):
        # form の上限を下回ると、拒否対象のファイルが握り潰されて
        # 「画像サイズが大きすぎます」を返せなくなる
        self.assertGreater(
            settings.FILE_UPLOAD_MAX_MEMORY_SIZE, forms.MAX_IMAGE_SIZE
        )

    @override_settings(VIEW_TEST_MODE=True)
    def test_oversized_upload_leaves_no_file_on_disk(self):
        """FILE_UPLOAD_MAX_MEMORY_SIZE 超の画像を POST しても一時ファイルが作られない。

        「処理後に消えている」だけでは、一度ディスクに書いてから消す実装でも
        通ってしまう。そもそも TemporaryUploadedFile が生成されないことも見る。
        """
        created = []
        original_init = uploadedfile.TemporaryUploadedFile.__init__

        def spy_init(temp_file, *args, **kwargs):
            original_init(temp_file, *args, **kwargs)
            created.append(temp_file.temporary_file_path())

        png = png_at_least(settings.FILE_UPLOAD_MAX_MEMORY_SIZE + 1)

        with tempfile.TemporaryDirectory() as temp_dir:
            with override_settings(FILE_UPLOAD_TEMP_DIR=temp_dir):
                with mock.patch.object(
                    uploadedfile.TemporaryUploadedFile, "__init__", spy_init
                ):
                    response = self.client.post(
                        "/", {"mode": "image", "text": "", "image": upload(png)}
                    )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                sorted(os.listdir(temp_dir)),
                [],
                "リクエスト処理後も一時ディレクトリにファイルが残っている",
            )

        self.assertEqual(
            created,
            [],
            "TemporaryUploadedFile が生成された＝入力画像が一度ディスクに書かれている",
        )

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


@override_settings(VIEW_TEST_MODE=False)
class LogsNeverContainInputTests(TestCase):
    """判定が失敗したときのログに、求人テキストが出ないことの検証。"""

    def _post_with_api_failure(self, error):
        """Claude API 呼び出しが error で失敗する状況で POST する。"""
        with mock.patch.object(service.anthropic, "Anthropic") as client_class:
            client_class.return_value.messages.parse.side_effect = error
            with capture_logs() as logs:
                response = self.client.post("/", {"mode": "text", "text": JOB_TEXT})
        # 判定は失敗し、ユーザーには汎用メッセージが出る
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "判定に失敗しました")
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
