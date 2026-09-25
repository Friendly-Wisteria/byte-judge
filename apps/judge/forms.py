from django import forms

from .consultation import CONSULTATION_GUIDE

# 画像（スクリーンショット）入力は停止中。
# 以下の定数は再開時にそのまま使えるよう残している（apps/judge/tests/ の
# @skip 済みテストが参照する）。停止の経緯は README を参照。
# なお service.py 側の画像処理は、到達しなくなるが残置している。
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB
MAX_IMAGE_PIXELS = 33_000_000  # 約33MP（8Kスクショまで許容。実機を超える解像度は拒否）

OVERSIZED_IMAGE_ERROR = "画像サイズが大きすぎます。5MB以下の画像を選んでください。"
# 画像がフォームに届く前に捨てられた場合の文言。
# 原因はリクエスト全体のサイズなので、画像単体の上限とは別の案内にする。
OVERSIZED_REQUEST_ERROR = (
    "画像とテキストの合計サイズが大きすぎます。"
    "画像を小さくするか、募集文を短くしてお試しください。"
)

NO_INPUT_ERROR = "募集文を入力してください。"

# 募集文の上限。普通の求人で長い部類が2,300文字程度という仮定に、マージンを
# 持たせた値（内訳の仮定は #29 の対応コミットを参照）。
TEXT_MAX_LENGTH = 3000

# 上限を超えたときの案内。判定を返せない経路なので、views._unavailable() と
# 同じ「理由 / これからどうなるか / 相談先」の3段に揃え、相談先を末尾に付ける。
# 文字数は Django の max_length バリデータが渡す params で埋めるため、上限の
# 値をここに書き写さない。
# %(value)s は使わないこと（募集文そのものがメッセージに載り、messages 経由で
# cookie / session に乗る）。またこの文字列は % 補間を通るので、
# CONSULTATION_GUIDE 側にも % を入れないこと。
MAX_LENGTH_ERROR = (
    "募集文が長すぎるため、判定できませんでした（%(show_value)s文字）。"
    "%(limit_value)s文字以内にしてください。\n"
    "入力欄には、送った文章がそのまま残っています。仕事内容・報酬・連絡方法が"
    "書かれた部分を残して、要らない部分を削るか、募集文の部分だけを貼り直して"
    "ください。\n\n"
    + CONSULTATION_GUIDE
)


class NewlineNormalizedCharField(forms.CharField):
    """改行を LF に正規化してから、長さを数えるフィールド。

    ブラウザはフォーム送信時に改行を CRLF にする（HTML の仕様）。一方、画面の
    文字数の表示が使う textarea.value は改行を LF で数えるため、正規化しないと
    改行の数だけサーバー側が多く数え、「カウンタは上限内なのに弾かれる」ことに
    なる（#57）。利用者が見ている数え方に合わせる。

    正規化は to_python に置く。max_length のバリデータはこの後に走るので、
    clean_text で直しても間に合わない。
    """

    def to_python(self, value):
        value = super().to_python(value)
        if value in self.empty_values:
            return value
        return value.replace("\r\n", "\n").replace("\r", "\n")


class JobOfferRiskAssessForm(forms.Form):
    # 画像フィールドは意図的に定義しない。UI をコメントアウトしただけでは、
    # POST に image を含めれば判定まで通ってしまうため、受け口ごと閉じる。
    # 未知のフィールドは Django のフォームが無視するので、image を付けて
    # POST されても text だけで判定され、画像は読み捨てられる。
    text = NewlineNormalizedCharField(  # CharField は既定で strip 済み・required=True
        max_length=TEXT_MAX_LENGTH,
        widget=forms.Textarea,
        error_messages={
            "required": NO_INPUT_ERROR,
            "max_length": MAX_LENGTH_ERROR,
        },
    )
