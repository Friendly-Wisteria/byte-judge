from django import forms
from django.core.files.images import get_image_dimensions

# アップロード画像の制限（メモリ・処理負荷 / decompression bomb 対策）
MAX_IMAGE_SIZE = 5 * 1024 * 1024   # 5MB
MAX_IMAGE_PIXELS = 33_000_000       # 約33MP（8Kスクショまで許容。実機を超える解像度は拒否）

class JobOfferRiskAssessForm(forms.Form):
    image = forms.ImageField(required=False)
    text = forms.CharField(required=False, widget=forms.Textarea)

    def clean_image(self):
        image = self.cleaned_data.get('image')
        if not image:
            return image
        if image.size > MAX_IMAGE_SIZE:
            raise forms.ValidationError('画像サイズが大きすぎます。5MB以下の画像を選んでください。')
        # decompression bomb 対策：ヘッダからピクセル数のみを確認（フル展開しない）
        width, height = get_image_dimensions(image)
        image.seek(0)  # get_image_dimensions で進んだ読み取り位置を、後続の Image.open のため先頭へ戻す
        if width and height and width * height > MAX_IMAGE_PIXELS:
            raise forms.ValidationError('画像の解像度が大きすぎます。通常のスクリーンショットを使用してください。')
        return image

    def clean(self):
        cleaned_data = super().clean()
        image = cleaned_data.get('image')
        text = cleaned_data.get('text')  # CharFieldは既定でstrip済み
        # 画像がサイズ超過などで既にエラーの場合は、そのエラーを優先し重複表示を避ける
        if 'image' in self.errors:
            return cleaned_data
        if not image and not text:
            raise forms.ValidationError('スクショまたは募集文のどちらかを入力してください。')
        return cleaned_data
