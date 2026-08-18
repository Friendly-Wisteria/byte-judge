from django.db import models


class DailyUsage(models.Model):
    """1日に Claude API へ投げた判定の件数。

    全体の上限（quota.reserve_site_slot）を数えるためだけの行で、持つのは
    日付と件数だけ。求人テキスト・判定結果・利用者を特定できる情報は含まない。
    """

    date = models.DateField(unique=True, verbose_name="日付（JST）")
    count = models.PositiveIntegerField(default=0, verbose_name="判定件数")

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"{self.date}: {self.count}"
