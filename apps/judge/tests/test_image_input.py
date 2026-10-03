"""画像（スクリーンショット）入力の回帰テスト。

受け口は停止中だが、API に渡すための変換処理は service 側に残している
（README のとおり再開予定）。停止に依存するテストだけ @skip で眠らせる。
"""

import base64
import io
from unittest import mock, skip

from django.conf import settings
from django.test import TestCase, override_settings
from PIL import Image

from .. import forms, service, views
from ..fixtures import FIXTURES
from ..schema import RiskReportSchema
from .helpers import IMAGE_PAUSED, JOB_TEXT, png_at_least, upload


@skip(IMAGE_PAUSED)
@override_settings(VIEW_TEST_MODE=True)
class DiscardedOversizedImageIsReportedTests(TestCase):
    """リクエスト全体のサイズ超過で画像が捨てられた場合の案内の検証。

    メモリ専用ハンドラ構成では、上限超過の画像は request.FILES に載らず
    黙って捨てられる。フォームからは「画像が選ばれていない」状態と区別が
    つかないため、実態と違うエラー（あるいは、テキストだけの判定）に
    なっていないかを見る。
    """

    def test_data_limit_stays_within_the_file_memory_limit(self):
        # 検知の前提：ファイル以外のフィールドはこちらの上限で別に制限される。
        # 逆転すると「超過分＝画像」と言えなくなり、検知が誤判定になる。
        self.assertLessEqual(
            settings.DATA_UPLOAD_MAX_MEMORY_SIZE, settings.FILE_UPLOAD_MAX_MEMORY_SIZE
        )

    def test_image_with_long_text_reports_the_combined_size(self):
        """画像＋長文で上限を超えた場合、合計サイズが原因だと分かること。"""
        png = png_at_least(4 * 1024 * 1024)
        # 画像と合わせて FILE_UPLOAD_MAX_MEMORY_SIZE を超える長さ（"あ" は 3 バイト）
        text = "あ" * ((settings.FILE_UPLOAD_MAX_MEMORY_SIZE - len(png)) // 3 + 1000)
        self.assertLess(len(png), forms.MAX_IMAGE_SIZE)  # 画像単体では上限内
        # テキスト側は上限内（＝413 ではなく、画像破棄の経路を通る）
        self.assertLess(len(text.encode()), settings.DATA_UPLOAD_MAX_MEMORY_SIZE)

        response = self.client.post(
            "/", {"mode": "image", "text": text, "image": upload(png)}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, forms.OVERSIZED_REQUEST_ERROR)
        # 画像が消えたまま、テキストだけで判定して結果を出していないこと
        self.assertIsNone(response.context.get("result"))

    def test_oversized_image_alone_reports_the_image_size(self):
        """画像だけで上限を超えた場合、「入力が無い」ではなく画像サイズを案内すること。"""
        png = png_at_least(settings.FILE_UPLOAD_MAX_MEMORY_SIZE + 1)

        response = self.client.post(
            "/", {"mode": "image", "text": "", "image": upload(png)}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, forms.OVERSIZED_IMAGE_ERROR)
        self.assertNotContains(response, "どちらかを入力してください")


@override_settings(VIEW_TEST_MODE=True)
class ImageInputIsClosedTests(TestCase):
    """画像入力の受け口が閉じていることの検証。

    UI をコメントアウトしただけでは、POST に image を含めれば判定まで通って
    しまう。フォームのフィールドごと閉じたことを、実際にリクエストを通して
    確認する（画像機能を再開するときは、このクラスを削除する）。
    """

    def test_form_has_no_image_field(self):
        self.assertNotIn("image", forms.JobOfferRiskAssessForm().fields)

    def test_image_only_post_is_not_judged(self):
        """画像だけを POST しても判定されず、募集文を求められること。"""
        response = self.client.post(
            "/", {"mode": "image", "image": upload(png_at_least(2000))}
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context.get("result"))
        self.assertContains(response, forms.NO_INPUT_ERROR)

    def test_image_sent_with_text_is_ignored(self):
        """テキストに画像を添えて POST しても、判定に渡るのはテキストだけであること。"""
        report = RiskReportSchema.model_validate(FIXTURES["danger"])
        with mock.patch.object(
            views, "job_offer_risk_assess", return_value=report
        ) as assess:
            self.client.post(
                "/",
                {
                    "mode": "image",
                    "text": JOB_TEXT,
                    "image": upload(png_at_least(2000)),
                },
            )

        (passed,) = assess.call_args.args
        self.assertIsInstance(passed, str)
        self.assertEqual(passed, JOB_TEXT)

    def test_empty_submission_reports_the_missing_input(self):
        """何も入力されていない場合は、募集文の入力を案内すること。"""
        response = self.client.post("/", {"mode": "text", "text": ""})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, forms.NO_INPUT_ERROR)


@override_settings(VIEW_TEST_MODE=False)
class ImageIsConvertedForTheApiTests(TestCase):
    """画像を API に渡せる形に変換する処理の検証。

    画像入力の UI とフォームは停止中だが、この変換は service 側に残してあり
    （README のとおり再開予定）、フォームを通らずに呼べる。止めている間に
    黙って壊れないよう、UI に依存しないこの部分は常時実行する。フォームの
    画像フィールドが前提のテストは、@skip のまま眠らせておく。
    """

    def _sent_content(self, image):
        with mock.patch.object(service.anthropic, "Anthropic") as client_class:
            parse = client_class.return_value.messages.parse
            parse.return_value = mock.Mock(stop_reason="end_turn")
            service.job_offer_risk_assess(image)
        return parse.call_args.kwargs["messages"][0]["content"]

    def _decode(self, block):
        data = base64.standard_b64decode(block["source"]["data"])
        return Image.open(io.BytesIO(data))

    def test_an_image_is_sent_as_an_inline_png_block(self):
        block, caption = self._sent_content(Image.new("RGB", (20, 10), "red"))

        self.assertEqual(block["type"], "image")
        self.assertEqual(block["source"]["type"], "base64")
        self.assertEqual(block["source"]["media_type"], "image/png")
        self.assertEqual(self._decode(block).format, "PNG")
        self.assertEqual(caption["text"], "# 評価対象の求人")

    def test_a_long_edge_beyond_the_limit_is_scaled_down(self):
        """API 側で自動縮小される大きさに、送る前に合わせること。"""
        limit = service.MAX_IMAGE_LONG_EDGE
        cases = {
            "上限超": ((limit * 2, limit), (limit, limit // 2)),
            "上限内": ((limit - 100, 10), (limit - 100, 10)),
        }
        for label, (size, expected) in cases.items():
            with self.subTest(size=label):
                content = self._sent_content(Image.new("RGB", size, "red"))

                self.assertEqual(self._decode(content[0]).size, expected)

    def test_a_mode_png_cannot_hold_is_converted(self):
        """PNG で保存できないモード（CMYK 等）でも送れること。"""
        content = self._sent_content(Image.new("CMYK", (20, 10)))

        self.assertEqual(self._decode(content[0]).mode, "RGB")

    def test_an_exif_rotation_is_applied(self):
        """横倒しのまま送らないこと（読み取り精度が落ちるため）。"""
        exif = Image.Exif()
        exif[274] = 6  # Orientation: 90度回転
        buffer = io.BytesIO()
        Image.new("RGB", (40, 20), "red").save(buffer, format="JPEG", exif=exif)

        content = self._sent_content(Image.open(io.BytesIO(buffer.getvalue())))

        self.assertEqual(self._decode(content[0]).size, (20, 40))

    def test_a_conversion_failure_is_reported_as_failed(self):
        """変換で例外が出ても、500 にせず判定不可として返すこと。"""
        with (
            mock.patch.object(
                service, "_pil_to_image_block", side_effect=OSError("broken")
            ),
            mock.patch.object(service.anthropic, "Anthropic") as client_class,
        ):
            result = service.job_offer_risk_assess(Image.new("RGB", (10, 10)))

        self.assertIs(result, service.AssessmentError.FAILED)
        client_class.return_value.messages.parse.assert_not_called()
