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
from django.test import SimpleTestCase, TestCase, override_settings

from ..consultation import CONSULTATION_CONTACTS, CONSULTATION_HEADING
from ..forms import JobOfferRiskAssessForm
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


@override_settings(VIEW_TEST_MODE=True)
class TextareaMaxLengthTest(SimpleTestCase):
    """フォームの文字数の上限3,000文字を超えた場合に、リクエストが拒否されることを確認する。"""

    MAX_LENGTH = 3000

    def _form(self, length):
        filler = MARKER + "あ" * (length)
        form = JobOfferRiskAssessForm(data={"text": filler[:length]})
        return form

    def test_max_acceptable_letter_count_is_3000(self):
        """3,000文字ぴったりの長さのテキストは通す
        変異テスト: `JobOfferRiskAssessForm`の`max_length`を短くする
        """
        form = self._form(3000)
        self.assertTrue(form.is_valid())

    def test_text_excess_3000_letters_is_invalid(self):
        """3,000文字を一文字でも超えると通さない
        変異テスト: `JobOfferRiskAssessForm`の`max_length`を長くする
        """
        form = self._form(3001)
        self.assertFalse(form.is_valid())

    def test_error_message_contains_guide_to_consultation(self):
        """文字数を超過した時に、公的な相談窓口の案内を表示する"""
        form = self._form(4000)
        self.assertEqual(len(form.errors["text"]), 1)
        self.assertIn(CONSULTATION_HEADING, form.errors["text"][0])
        for contact, case in CONSULTATION_CONTACTS:
            with self.subTest(contact=contact):
                self.assertIn(contact, form.errors["text"][0])
