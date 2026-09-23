"""リクエスト全体が大きすぎたときに、利用者へ何が見えるかの回帰テスト。

テキストだけでも DATA_UPLOAD_MAX_MEMORY_SIZE を超えると、Django が view に
届く前に弾く（RequestDataTooBig は SuspiciousOperation なので 413 ではなく
400 になる）。このとき出るのはアプリの画面ではないため、相談先の案内も
入力の直し方も届かない。ここでは現状の見え方を固定したうえで、少なくとも
「500 にしない」「貼り付けた求人文をページに出さない」ことを守る。

画像を含む場合の経路は test_image_input.py にある（画像入力の停止中は skip）。
"""

import urllib.parse

from django.conf import settings
from django.test import TestCase, override_settings

from .helpers import JOB_TEXT, MARKER


@override_settings(VIEW_TEST_MODE=True)
class OversizedTextIsRefusedBeforeTheViewTests(TestCase):
    def _post_oversized_text(self):
        # テキスト側のフォームは multipart ではないので、実際の経路に合わせて
        # urlencoded で送る。"あ" は URL エンコード後 9 バイトになる。
        filler = "あ" * (settings.DATA_UPLOAD_MAX_MEMORY_SIZE // 9 + 1000)
        body = urllib.parse.urlencode({"mode": "text", "text": JOB_TEXT + filler})
        # 前提：本当に上限を超えていること（超えていなければ検知にならない）
        self.assertGreater(len(body.encode()), settings.DATA_UPLOAD_MAX_MEMORY_SIZE)
        return self.client.post(
            "/", data=body, content_type="application/x-www-form-urlencoded"
        )

    def test_the_request_is_refused_without_a_server_error(self):
        """落ちずに 400 で断ること。"""
        self.assertEqual(self._post_oversized_text().status_code, 400)

    def test_the_pasted_job_text_is_not_echoed_back(self):
        """弾かれたときのページに、貼り付けた求人文が出ないこと。"""
        self.assertNotContains(self._post_oversized_text(), MARKER, status_code=400)

    def test_the_user_currently_sees_djangos_default_page(self):
        """現状の固定：アプリの画面ではなく、Django 既定の 400 が出ている。

        つまり相談先も入力の直し方も届いていない。利用者向けの案内
        （handler400 か、送信前の長さ制限）を用意したら、このテストは
        その案内を確かめる形に書き換える。
        """
        self.assertNotContains(
            self._post_oversized_text(), "バイトジャッジ", status_code=400
        )
