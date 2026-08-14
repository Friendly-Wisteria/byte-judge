"""「入力内容をサーバー側に残さない」ことの回帰テスト。

設定値の目視確認では将来の変更で退行しても気づけないため、
実際にリクエストを通して外部に残る場所（ディスク / ログ / セッション）を検査する。
"""

import io
import math
import os
import tempfile
from unittest import mock

from django.conf import settings
from django.core.files import uploadedfile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image

from . import forms, views

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
