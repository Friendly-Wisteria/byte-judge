from enum import StrEnum
from typing import Annotated

from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
    computed_field,
    model_validator,
)

# 画面の主役になる文字列。min_length は文字数しか見ないため、空白だけの
# 文字列（" " や改行）は長さ1として通ってしまう。前後の空白を取り除いてから
# 長さを見ることで、画面に空の枠が出る状態を弾く。
NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Level(StrEnum):
    DANGER = "危険"
    CAUTION = "要注意"
    SAFE = "安全"


class Severity(StrEnum):
    HIGH = "高"
    MID = "中"
    LOW = "低"


class MissingInfo(StrEnum):
    """判定に必要だが、貼り付けられたテキストに見当たらなかった項目。"""

    OPERATOR = "事業者情報"
    JOB_DETAIL = "仕事内容"
    PAY = "報酬・給与条件"
    WORKPLACE = "勤務地・勤務時間"
    CONTACT = "応募・連絡方法"


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
# 画面に出す判定ラベル。「安全」と言い切らず、確認できた範囲の結果として示す
# （偽陰性だったとき、断定は取り返しがつかないため）
_LEVEL_LABEL = {
    Level.DANGER: "危険",
    Level.CAUTION: "要注意",
    Level.SAFE: "危険な兆候なし",
}

# 情報不足のときの表示（判定名・判定色を使わない）
INSUFFICIENT_LABEL = "情報不足"
INSUFFICIENT_COLOR = "secondary"

# 不足項目 → 「何を貼り足せばよいか」の案内（表示専用）
_MISSING_HINT = {
    MissingInfo.OPERATOR: "会社名・所在地・電話番号・許可番号など、募集元がわかる部分",
    MissingInfo.JOB_DETAIL: "実際に何をする仕事なのかが書かれている部分",
    MissingInfo.PAY: "時給・日給・支払い方法が書かれている部分",
    MissingInfo.WORKPLACE: "勤務地・勤務時間・雇用形態が書かれている部分",
    MissingInfo.CONTACT: "応募方法や連絡先（アプリ名・URLなど）が書かれている部分",
}


class Signal(BaseModel):
    # name と detail は空でも受け取る（summary / advice とは意図して揃えていない）。
    # 片方が空でも、もう一方が読めれば利用者は何かを持ち帰れる。空の兆候が1件
    # 混ざっただけで判定ごと失うほうが損失が大きいので、関所では止めない。
    # 「signals はあるが全項目が空」だけは落としたいが、起こる確率に対して検知が
    # 重いため入れていない（#77）。
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
    summary: NonBlankText = Field(description="総合判断を1〜2文で")
    signals: list[Signal] = Field(default_factory=list, description="検出シグナル")
    advice: NonBlankText = Field(description="推奨アクションを1〜2文で")
    has_enough_info: bool = Field(
        description="闇バイトかどうかを判断できるだけの情報が、与えられたテキストに含まれていたか"
    )
    missing_info: list[MissingInfo] = Field(
        default_factory=list,
        description="判定に必要だが、テキストに見当たらなかった項目",
    )

    @computed_field
    @property
    def bs_color(self) -> str:
        # 情報が足りないまま「安全（緑）」を出すと、判断が付いたように読めてしまう。
        # 判定の3色は使わず、中立の配色にする。
        if not self.has_enough_info:
            return INSUFFICIENT_COLOR
        return _LEVEL_COLOR[self.level]

    @computed_field
    @property
    def level_label(self) -> str:
        """画面に出す判定ラベル。情報不足のときは判定名を出さない。"""
        if not self.has_enough_info:
            return INSUFFICIENT_LABEL
        return _LEVEL_LABEL[self.level]

    @model_validator(mode="after")
    def _reconcile_missing_info(self):
        """不足項目と十分フラグの矛盾をならす。

        不足項目を挙げながら has_enough_info=true を返してくることがある。
        画面では「暫定表示」と「不足の案内」が同じフラグで動くため、
        矛盾したまま出さず、安全側（不足あり）に寄せる。重複も畳む。
        """
        self.missing_info = list(dict.fromkeys(self.missing_info))
        if self.missing_info:
            self.has_enough_info = False
        return self

    @property
    def missing_info_hints(self) -> list[dict[str, str]]:
        """不足項目を {ラベル, 補足} にして返す（表示専用）。"""
        return [
            {"label": item.value, "hint": _MISSING_HINT[item]}
            for item in self.missing_info
        ]
