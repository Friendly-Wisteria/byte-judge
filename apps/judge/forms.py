from django import forms

# 画像（スクリーンショット）入力は停止中。
# 以下の定数は再開時にそのまま使えるよう残している（apps/judge/tests.py の
# @skip 済みテストが参照する）。停止の経緯は README を参照。
# なお service.py 側の画像処理は、到達しなくなるが残置している。
MAX_IMAGE_SIZE = 5 * 1024 * 1024   # 5MB
MAX_IMAGE_PIXELS = 33_000_000       # 約33MP（8Kスクショまで許容。実機を超える解像度は拒否）

OVERSIZED_IMAGE_ERROR = '画像サイズが大きすぎます。5MB以下の画像を選んでください。'
# 画像がフォームに届く前に捨てられた場合の文言。
# 原因はリクエスト全体のサイズなので、画像単体の上限とは別の案内にする。
OVERSIZED_REQUEST_ERROR = (
    '画像とテキストの合計サイズが大きすぎます。'
    '画像を小さくするか、募集文を短くしてお試しください。'
)

NO_INPUT_ERROR = '募集文を入力してください。'


class JobOfferRiskAssessForm(forms.Form):
    # 画像フィールドは意図的に定義しない。UI をコメントアウトしただけでは、
    # POST に image を含めれば判定まで通ってしまうため、受け口ごと閉じる。
    # 未知のフィールドは Django のフォームが無視するので、image を付けて
    # POST されても text だけで判定され、画像は読み捨てられる。
    text = forms.CharField(  # CharField は既定で strip 済み・required=True
        widget=forms.Textarea,
        error_messages={'required': NO_INPUT_ERROR},
    )
