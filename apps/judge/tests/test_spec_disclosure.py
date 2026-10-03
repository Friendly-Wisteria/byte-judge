"""入力画面の、外部送信の説明の回帰テスト。

入力を始める前に伝えるべき内容なので、消えたり薄まったりしたら気づけるようにする。
"""

from django.test import SimpleTestCase


class ExternalTransferNoticeTests(SimpleTestCase):
    """入力画面の、外部送信の説明の検証。

    入力を始める前に伝えるべき内容なので、消えたり薄まったりしたら気づける
    ようにしておく。あわせて、入力前の同意チェックボックスを付けない方針も
    ここで固定する（入力前の摩擦が離脱を生み、それ自体が安全上の損失になる）。
    """

    def test_page_explains_the_transfer_to_an_external_service(self):
        response = self.client.get("/")

        self.assertContains(response, "外部のAIサービス（Anthropic社／アメリカ）に送信")
        self.assertContains(response, "通常は最大30日間保持されます")
        self.assertContains(response, "このサービスのサーバーには保存しません")

    def test_page_warns_against_entering_personal_details(self):
        response = self.client.get("/")

        self.assertContains(
            response, "氏名・住所・電話番号・口座番号などは入力しないでください"
        )

    def test_page_shows_the_flow_of_the_data(self):
        """データの流れが、画像ではなく本文として読める形で出ていること。"""
        response = self.client.get("/")

        for node in (
            "あなたが入力した内容",
            "バイトジャッジのサーバー",
            "Anthropic（AI判定）",
            "結果を画面に表示",
        ):
            with self.subTest(node=node):
                self.assertContains(response, node)
        self.assertContains(response, "保存しません")
        self.assertContains(response, "通常は最大30日で削除")

    def test_no_consent_checkbox_is_placed_before_input(self):
        response = self.client.get("/")

        self.assertNotContains(response, 'type="checkbox"')
