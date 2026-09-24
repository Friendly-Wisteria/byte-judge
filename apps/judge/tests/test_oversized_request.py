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
from ..forms import TEXT_MAX_LENGTH, JobOfferRiskAssessForm
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

        送信前の長さ制限（文字数の表示）とフォームの max_length を入れたため、
        通常の利用でこの 400 に届くことはなくなった。ここに来るのはテキスト欄
        に約29万文字（DATA_UPLOAD_MAX_MEMORY_SIZE を urlencoded で超える量）を
        送った場合だけで、非テキストの巨大な POST は 200 でアプリの画面が返る
        （ファイル部分にこの上限は効かず、画像フィールドも無いため読み捨て）。

        handler400 を用意するかは、画像入力の再開と合わせて判断する。この上限は
        画像が捨てられたことの検知の前提でもあり（test_image_input.py の
        test_data_limit_stays_within_the_file_memory_limit）、単独で動かせない。
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

    def test_error_message_reports_the_length_and_the_limit(self):
        """何文字だったか・何文字までかを、両方そのまま出す
        変異テスト: メッセージから %(show_value)s / %(limit_value)s を落とす
        """
        message = self._form(4000).errors["text"][0]
        self.assertIn("4000文字", message)
        self.assertIn(f"{self.MAX_LENGTH}文字以内", message)

    def test_error_message_does_not_echo_the_job_text(self):
        """募集文そのものはメッセージに載せない
        （messages は cookie / session に保存されるため）
        変異テスト: メッセージに %(value)s を入れる
        """
        self.assertNotIn(MARKER, self._form(4000).errors["text"][0])


@override_settings(VIEW_TEST_MODE=True)
class TheInputIsHandedBackTests(TestCase):
    """弾かれても貼り直しにならないこと、上限が HTML 側にも出ていることの検証。

    入力欄を空に戻してしまうと、「短くしてもう一度」と案内しても、短くする元の
    文章が利用者の手元に無い。maxlength のほうは、送信前に止めることで無駄な
    往復をなくすためのもの（超過ぶんは黙って捨てられるため、文字数の表示と
    セットで意味を持つ。その表示は JS なのでここでは検証できない）。
    """

    def test_the_submitted_text_comes_back_in_the_textarea(self):
        """長すぎて弾かれても、送った募集文はそのまま入力欄に戻る
        変異テスト: textarea の中身を空に戻す
        """
        response = self.client.post("/", {"mode": "text", "text": MARKER + "あ" * 4000})
        self.assertContains(response, MARKER)

    def test_the_text_survives_a_successful_judgment(self):
        """判定できたときも残す（見当たらなかった項目を貼り足せるようにするため）
        変異テスト: 上と同じ
        """
        response = self.client.post("/", {"mode": "text", "text": JOB_TEXT})
        self.assertContains(response, MARKER)

    def test_the_textarea_does_not_cap_the_input(self):
        """textarea に maxlength を付けない

        ブラウザは超過ぶんを黙って捨てるため、上限で止めると、残したい末尾
        （連絡方法が書かれやすい場所）を貼り足す手段がなくなる。3,000文字に
        絞る作業のために外部のエディタを開かせることになるので、入力自体は
        止めず、文字数の表示とサーバー側の max_length で受ける。
        変異テスト: textarea に maxlength を付ける
        """
        self.assertNotContains(self.client.get("/"), "maxlength")

    def test_the_consultation_guide_reaches_the_page(self):
        """上限超過で判定を返せないときも、相談先が画面に出る

        フォーム単体の検証（TextareaMaxLengthTest）はメッセージの文字列までしか
        見ないため、画面に届いたかはこちらで見る。なお送信前に JS で止めると
        この経路ごと消えるが、テストに JS はいないので検知できない。だから
        上限超過は JS で止めない。
        変異テスト: MAX_LENGTH_ERROR から CONSULTATION_GUIDE を外す
        """
        response = self.client.post("/", {"mode": "text", "text": "あ" * 4000})
        for contact, _ in CONSULTATION_CONTACTS:
            with self.subTest(contact=contact):
                self.assertContains(response, contact)

    def test_the_limit_reaches_the_html(self):
        """文字数の表示が使う上限は、forms の max_length から取る
        変異テスト: data-max-length を数字で書く / 落とす
        """
        self.assertContains(
            self.client.get("/"), f'data-max-length="{TEXT_MAX_LENGTH}"'
        )
