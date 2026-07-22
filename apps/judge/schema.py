from enum import StrEnum
from pydantic import BaseModel,Field,computed_field

class Level(StrEnum):
    DANGER = "危険"
    CAUTION = "要注意"
    SAFE = "安全"

class Severity(StrEnum):
    HIGH = "高"
    MID = "中"
    LOW = "低"

# 表示ラベル → Bootstrap 配色クラスの対応表（表示専用）
_LEVEL_COLOR = {
    Level.DANGER: "danger",
    Level.CAUTION: "warning",
    Level.SAFE: "success",
}
_SEVERITY_COLOR = {
    Severity.HIGH: "danger",
    Severity.MID: "warning",
    Severity.LOW: "secondary",
}

class Signal(BaseModel):
    name: str = Field(description="シグナル名")
    severity: Severity = Field(description="深刻度")
    detail: str = Field(description="そう判断した根拠")

    @computed_field
    @property
    def bs_color(self) -> str:
        return _SEVERITY_COLOR[self.severity]


class RiskReportSchema(BaseModel):
    score: int = Field(ge=0, le=100, description="危険度 0〜100")
    level: Level = Field(description="総合判定ラベル")
    summary: str = Field(description="総合判断を1〜2文で")
    signals: list[Signal] = Field(default_factory=list, description="検出シグナル")
    advice: str = Field(description="推奨アクションを1〜2文で")

    @computed_field
    @property
    def bs_color(self) -> str:
        return _LEVEL_COLOR[self.level]
