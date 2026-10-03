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

    def test_the_consultation_guide_reaches_this_page_too(self):
        """view に届かないこの経路でも、相談先が出ること（#50）。

        送信前の長さ制限（文字数の表示）とフォームの max_length を入れたため、
        通常の利用でこの 400 に届くことはない。ここに来るのはテキスト欄に約29万
        文字（DATA_UPLOAD_MAX_MEMORY_SIZE を urlencoded で超える量）を送った
        場合だけだが、届いた人に案内が無いままにはしない。

        画面そのものの検証は test_spec_error_pages.py にある。ここでは「この経路から
        あの画面に届く」ことだけを見る。
        変異テスト: config.urls の handler400 を外す
        """
        response = self._post_oversized_text()

        for contact, _ in CONSULTATION_CONTACTS:
            with self.subTest(contact=contact):
                self.assertContains(response, contact, status_code=400)


@override_settings(VIEW_TEST_MODE=True)
class TextareaMaxLengthTest(SimpleTestCase):
    """フォームの文字数の上限を超えた場合に、リクエストが拒否されることを確認する。

    上限の値そのものはここに書き写さない（forms との二重管理になり、上限を
    動かしたときに、超過を期待しているテストが黙って通るようになる）。
    """

    OVER_LENGTH = TEXT_MAX_LENGTH + 1000

    def _form(self, length):
        filler = MARKER + "あ" * (length)
        form = JobOfferRiskAssessForm(data={"text": filler[:length]})
        return form

    def test_text_at_the_limit_is_accepted(self):
        """上限ぴったりの長さのテキストは通す
        変異テスト: `JobOfferRiskAssessForm`の`max_length`を短くする
        """
        form = self._form(TEXT_MAX_LENGTH)
        self.assertTrue(form.is_valid())

    def test_text_over_the_limit_is_invalid(self):
        """上限を一文字でも超えると通さない
        変異テスト: `JobOfferRiskAssessForm`の`max_length`を長くする
        """
        form = self._form(TEXT_MAX_LENGTH + 1)
        self.assertFalse(form.is_valid())

    def test_crlf_newlines_are_counted_as_one_character(self):
        """改行は、画面の文字数の表示と同じく1文字として数える

        ブラウザは送信時に改行を CRLF にする（HTML の仕様）。一方、表示が使う
        textarea.value は LF で数えるため、正規化しないと改行の数だけ多く数え、
        「カウンタは上限内なのに弾かれる」ことになる（#57）。
        変異テスト: `NewlineNormalizedCharField.to_python` の正規化を外す
        """
        block = "あ" * 9
        lines = TEXT_MAX_LENGTH // 10
        # LF 換算でちょうど上限（末尾は改行で終わらせない。CharField が strip する）
        text = ("\n".join([block] * lines)) + "あ"
        self.assertEqual(len(text), TEXT_MAX_LENGTH)

        submitted = text.replace("\n", "\r\n")
        # 前提：正規化しなければ超えること（超えなければ検知にならない）
        self.assertGreater(len(submitted), TEXT_MAX_LENGTH)

        self.assertTrue(JobOfferRiskAssessForm(data={"text": submitted}).is_valid())

    def test_the_judged_text_has_normalized_newlines(self):
        """判定に渡る本文の改行も LF に揃える（数えた対象と同じものを送る）
        変異テスト: 正規化を to_python ではなく max_length の後ろに移す
        """
        form = JobOfferRiskAssessForm(data={"text": "前半\r\n後半\r末尾"})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["text"], "前半\n後半\n末尾")

    def test_error_message_contains_guide_to_consultation(self):
        """文字数を超過した時に、公的な相談窓口の案内を表示する"""
        form = self._form(self.OVER_LENGTH)
        self.assertEqual(len(form.errors["text"]), 1)
        self.assertIn(CONSULTATION_HEADING, form.errors["text"][0])
        for contact, case in CONSULTATION_CONTACTS:
            with self.subTest(contact=contact):
                self.assertIn(contact, form.errors["text"][0])

    def test_error_message_reports_the_length_and_the_limit(self):
        """何文字だったか・何文字までかを、両方そのまま出す
        変異テスト: メッセージから %(show_value)s / %(limit_value)s を落とす
        """
        message = self._form(self.OVER_LENGTH).errors["text"][0]
        self.assertIn(f"{self.OVER_LENGTH}文字", message)
        self.assertIn(f"{TEXT_MAX_LENGTH}文字以内", message)

    def test_error_message_does_not_echo_the_job_text(self):
        """募集文そのものはメッセージに載せない
        （messages は cookie / session に保存されるため）
        変異テスト: メッセージに %(value)s を入れる
        """
        self.assertNotIn(MARKER, self._form(self.OVER_LENGTH).errors["text"][0])


@override_settings(VIEW_TEST_MODE=True)
class TheInputIsHandedBackTests(TestCase):
    """弾かれても貼り直しにならないこと、上限が HTML 側に届いていることの検証。

    入力欄を空に戻してしまうと、「短くしてもう一度」と案内しても、短くする元の
    文章が利用者の手元に無い。textarea に maxlength は付けないため（超過ぶんが
    黙って捨てられ、残したい末尾を貼り足せなくなる）、上限で止めるのはサーバー
    側だけで、JS は文字数の表示しか持たない。

    その表示に JS のテストは入れない判断をしている（#20）。故障しても失われる
    のはカウンタの表示だけで、相談先の案内・上限の enforcement・入力の復元・
    送信は、いずれも JS に依存していない。守るべき約束が JS の側に無いので、
    テスト基盤を入れる費用に見合わない。JS が表示以外のことを始めたら、この
    判断は無効になる。
    """

    def test_the_submitted_text_comes_back_in_the_textarea(self):
        """長すぎて弾かれても、送った募集文はそのまま入力欄に戻る
        変異テスト: textarea の中身を空に戻す
        """
        response = self.client.post(
            "/", {"mode": "text", "text": MARKER + "あ" * (TEXT_MAX_LENGTH + 1000)}
        )
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
        （連絡方法が書かれやすい場所）を貼り足す手段がなくなる。上限まで
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
        response = self.client.post(
            "/", {"mode": "text", "text": "あ" * (TEXT_MAX_LENGTH + 1000)}
        )
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
