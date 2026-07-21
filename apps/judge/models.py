from django.db import models
from encrypted_model_fields.fields import EncryptedTextField # 変更点
from django.utils.translation import gettext_lazy as _
from pydantic import ValidationError
import uuid
import json
from .schema import ConfigSchema

# Create your models here.
class JudgeConfig(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    updated_datetime = models.DateTimeField(_('更新日時'), auto_now=True)
    encrypted_config = EncryptedTextField(blank=False,null=False)
    @property
    def config(self):
        try:
            decrypted_config = json.loads(self.encrypted_config)
        except json.JSONDecodeError:
            return ConfigSchema()

        if isinstance(decrypted_config,str):
            # decode結果がstrなら、JSON文字列として保存された文字列とみなして、再度decode
            try:
                decrypted_config = json.loads(decrypted_config)
            except (json.JSONDecodeError,TypeError):
                return ConfigSchema()

        if isinstance(decrypted_config,dict):
            # 最終的にdictになったら、それをschemaでvalidation
            try:
                return ConfigSchema.model_validate(decrypted_config)
            except ValidationError:
                # validationできなければdefault値を返す
                return ConfigSchema()
        # dictになっていなければ、default値を返す
        return ConfigSchema()
    @config.setter
    def config(self,config_schema:ConfigSchema):
        self.encrypted_config = config_schema.model_dump_json()
