from django.apps import AppConfig


class JudgeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.judge"

    def ready(self):
        # decompression bomb 対策（多層防御）:
        # フォームを通らない経路（views の Image.open 等）も含めて、
        # 過大なピクセル数の画像をデコードさせないよう起動時に上限を設定する。
        from PIL import Image

        from .forms import MAX_IMAGE_PIXELS

        Image.MAX_IMAGE_PIXELS = (
            MAX_IMAGE_PIXELS  # 約33MP。この2倍超で Pillow が例外送出
        )
