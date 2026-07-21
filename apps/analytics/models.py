from django.db import models
from django.utils.translation import gettext_lazy as _
import uuid
import json
from pydantic import ValidationError
from judge.schema import RiskReportSchema

# Create your models here.
class JobOffer(models.Model):
    class DataType(models.TextChoices):
        IMAGE = 'image', _('画像')
        TEXT = 'text', _('テキスト')

    id = models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    created_datetime = models.DateTimeField(_('作成日時'),auto_now_add=True)
    data_type = models.CharField(
        _('データタイプ'),
        max_length=10,
        choices=DataType.choices,
        default=DataType.IMAGE
    )
    risk_report_json = models.JSONField(blank=True,null=True)

    @property
    def risk_report(self):
        if not self.risk_report_json:
            return None
        try:
            return RiskReportSchema.model_validate(self.risk_report_json)
        except ValidationError:
            return None

    @risk_report.setter
    def risk_report(self,risk_report:RiskReportSchema):
        self.risk_report_json = risk_report.model_dump(mode='json')
