"""テスト共通の道具。

収集対象（test*.py）から外すため、ファイル名を test で始めていない。
"""

import io
import logging
import math
import os

import anthropic
import pydantic
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from .. import views
from ..schema import RiskReportSchema

# 画像入力の停止にともない眠らせているテストの理由。機能を再開するときは
# この @skip を外せばそのまま使える（再開時に必要な修正は README を参照）。
IMAGE_PAUSED = "画像（スクリーンショット）入力は停止中。再開時にこの skip を外す。"

# 求人テキストに紛れ込ませる目印。ディスク・ログ・セッションのいずれにも
# 現れてはいけない。fixtures の文言と偶然一致しないよう一意な文字列にする。
MARKER = "ZZMARKER7f3a9cZZ"
JOB_TEXT = f"日給5万円・即日手渡し・Telegramで連絡ください 合言葉:{MARKER}"


def assert_consultation_is_offered(test, response):
    """判定不可の案内に、相談先が両方出ていることを確かめる。

    ページ下部の注意書きにも #9110 と 188 があるため、番号を探すだけでは
    案内側が空でも緑になる。案内は注意書きと書き分けてあるので、案内側の
    文言（かぎ括弧ではなく空白区切り）で確かめる。
    """
    test.assertIn("#9110", views.CONSULTATION_GUIDE)
    test.assertIn("188", views.CONSULTATION_GUIDE)
    test.assertContains(response, "判定をお届けできませんでした")
    test.assertContains(response, "警察相談専用ダイヤル #9110")
    test.assertContains(response, "消費者ホットライン 188（いやや）")


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

    LOGGER_NAMES = (
        "",
        "apps.judge",
        # 使用量のロガーは propagate=False なので、明示的に挙げないと
        # ここでの検査をすり抜ける（漏れがあっても緑になる）。
        "apps.judge.usage",
        "anthropic",
        "httpx",
        "django",
    )

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
    headers = {"request-id": "req_test"}
    request = FakeRequest()

    def __init__(self, status_code=400):
        self.status_code = status_code


def api_status_error():
    return anthropic.APIStatusError(
        "Invalid request",
        response=FakeResponse(),
        body={"error": {"type": "invalid_request_error", "message": "Invalid request"}},
    )


def rate_limit_error():
    """SDK の自動リトライ後もなおレート制限だった状況を再現する。"""
    return anthropic.RateLimitError(
        "Rate limited",
        response=FakeResponse(429),
        body={"error": {"type": "rate_limit_error", "message": "Rate limited"}},
    )


def billing_error():
    """月額の利用上限など、課金側で止められた状況を再現する。"""
    return anthropic.APIStatusError(
        "Billing error",
        response=FakeResponse(402),
        body={"error": {"type": "billing_error", "message": "spend limit reached"}},
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
                "has_enough_info": True,
            }
        )
    except pydantic.ValidationError as e:
        return e
    raise AssertionError("ValidationError を再現できていない")

# evaluate_prompt コマンドのテストで使うサンプルのテストセット。データ本体
# （cases.toml）はリポジトリに含めないため、テスト用に最小の構成を持つ。
EVAL_CASES_TOML = """[[case]]
id = "o1"
category = "obvious"
expect_signals = ["高額報酬|高すぎる報酬"]
text = "日給5万円 即日手渡し Telegram"

[[case]]
id = "l1"
category = "legitimate"
text = "コンビニスタッフ 時給1100円 株式会社◯◯"

[[case]]
id = "g1"
category = "gray"
text = "簡単な仕分け作業 日給1万5千円"
"""


def eval_cases_toml(**counts):
    """カテゴリごとの件数を指定して、最小限のテストセットを組み立てる。

    件数の制限や繰り返しの検証には、1カテゴリに複数件あるテストセットが要る。
    """
    blocks = []
    for category, count in counts.items():
        for nth in range(1, count + 1):
            blocks.append(
                f"[[case]]\n"
                f'id = "{category}-{nth}"\n'
                f'category = "{category}"\n'
                f'text = "募集文 {category} {nth}"\n'
            )
    return "\n".join(blocks)
