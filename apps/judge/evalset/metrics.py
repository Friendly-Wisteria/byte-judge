"""評価の指標。

単純な正解率は見ない。このサービスで最も重い失敗は「危険な求人を、危険で
ないと伝えてしまうこと」であり、正解率はそれを他の成功で薄めてしまうため。
"""

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from ..schema import Level
from .dataset import Case

# 利用者に警告が届いたと言える判定。「危険な兆候なし」と「情報不足」は、
# どちらも画面上は危険を伝えないので、届いていない扱いにする。
WARNING_LEVELS = frozenset({Level.DANGER, Level.CAUTION})


@dataclass
class Outcome:
    """1回の判定の結果。判定できなかった場合は error に理由が入る。"""

    case_id: str
    category: str
    error: str | None = None
    level: str | None = None
    label: str | None = None
    score: int | None = None
    has_enough_info: bool | None = None
    # シグナルの name と detail を連結したもの（根拠の照合に使う）
    signal_text: str = ""

    @property
    def judged(self) -> bool:
        return self.error is None

    @property
    def warned(self) -> bool:
        """利用者に危険が伝わる形で返ったか。"""
        if not self.judged or not self.has_enough_info:
            return False
        return self.level in WARNING_LEVELS


def outcome_from_report(case: Case, report) -> Outcome:
    signal_text = " ".join(f"{s.name} {s.detail}" for s in report.signals)
    return Outcome(
        case_id=case.id,
        category=case.category,
        level=report.level,
        label=report.level_label,
        score=report.score,
        has_enough_info=report.has_enough_info,
        signal_text=signal_text,
    )


@dataclass
class Ratio:
    """件数つきの割合。分母が0のときは割合を出さない（0% と紛らわしいため）。"""

    count: int = 0
    total: int = 0
    detail: dict = field(default_factory=dict)

    @property
    def rate(self) -> float | None:
        return self.count / self.total if self.total else None

    def format(self) -> str:
        if self.rate is None:
            return "対象なし"
        return f"{self.rate:.1%} ({self.count}/{self.total})"


def false_negative(outcomes: list[Outcome]) -> Ratio:
    """危険求人の見逃し率。最重視する指標。

    危険を伝えるべきケースのうち、画面上は危険が伝わらなかったものの割合。
    「危険な兆候なし」と「情報不足」の両方を見逃しに数える。利用者から見れば
    どちらも警告が出ていないため。
    """
    target = [o for o in outcomes if o.category in ("obvious", "disguised") and o.judged]
    missed = [o for o in target if not o.warned]
    return Ratio(
        count=len(missed),
        total=len(target),
        detail={
            "危険な兆候なしと判定": sum(
                1 for o in missed if o.has_enough_info and o.level == Level.SAFE
            ),
            "情報不足で判定を出せず": sum(1 for o in missed if not o.has_enough_info),
        },
    )


def underrated_obvious(outcomes: list[Outcome]) -> Ratio:
    """明らかな闇バイトを「要注意」止まりにした割合（参考値）。

    警告は届いているので見逃しではないが、強さが足りていない。
    """
    target = [o for o in outcomes if o.category == "obvious" and o.judged]
    return Ratio(
        count=sum(1 for o in target if o.warned and o.level == Level.CAUTION),
        total=len(target),
    )


def false_positive(outcomes: list[Outcome]) -> Ratio:
    """普通の求人を「危険」と判定した割合（参考値）。

    偽陽性は利用者を正規の仕事から遠ざけるが、偽陰性ほど重くはない。
    """
    target = [o for o in outcomes if o.category == "legitimate" and o.judged]
    return Ratio(
        count=sum(1 for o in target if o.level == Level.DANGER and o.has_enough_info),
        total=len(target),
    )


@dataclass
class Stability:
    """同じ入力を繰り返したときの、判定のぶれ。"""

    cases: int = 0
    label_agreement: float | None = None
    score_stdev: float | None = None
    split_cases: list[str] = field(default_factory=list)

    def format(self) -> str:
        if not self.cases:
            return "対象なし（繰り返し実行していない）"
        return (
            f"{self.cases}件を繰り返し実行 / "
            f"ラベル一致率 {self.label_agreement:.1%} / "
            f"スコアの標準偏差 平均 {self.score_stdev:.1f} / "
            f"判定が割れた {len(self.split_cases)}件"
        )


def stability(outcomes: list[Outcome], category: str | None = None) -> Stability:
    """繰り返し実行したケースについて、判定がぶれていないかを見る。

    画面に出るラベルで比べる。内部の判定値が同じでも、has_enough_info が
    ぶれれば利用者の見るものは変わるため。
    """
    grouped: dict[str, list[Outcome]] = defaultdict(list)
    for o in outcomes:
        if o.judged and (category is None or o.category == category):
            grouped[o.case_id].append(o)
    repeated = {cid: runs for cid, runs in grouped.items() if len(runs) >= 2}
    if not repeated:
        return Stability()

    agreements, stdevs, split = [], [], []
    for case_id, runs in sorted(repeated.items()):
        labels = Counter(o.label for o in runs)
        agreement = labels.most_common(1)[0][1] / len(runs)
        agreements.append(agreement)
        if agreement < 1.0:
            split.append(case_id)
        scores = [o.score for o in runs if o.score is not None]
        if len(scores) >= 2:
            stdevs.append(statistics.pstdev(scores))

    return Stability(
        cases=len(repeated),
        label_agreement=sum(agreements) / len(agreements),
        score_stdev=sum(stdevs) / len(stdevs) if stdevs else 0.0,
        split_cases=split,
    )


def signal_recall(cases: list[Case], outcomes: list[Outcome]) -> Ratio:
    """期待した根拠を、実際に挙げられていた割合。

    キーワードが挙げたシグナルの文中にあるかを見ているだけで、根拠の質そのもの
    を測ってはいない。取りこぼしに気づくための目安として使う。

    同じ根拠でもモデルの言い回しは揺れる（「高額報酬」と書くとは限らず、
    「高すぎる報酬」と書くこともある）。1つの期待に対して "|" 区切りで
    言い換えを並べられるようにして、言い回しの違いを取りこぼしと数えない。
    """
    expected = {c.id: c.expect_signals for c in cases if c.expect_signals}
    hit = total = 0
    misses: dict[str, list[str]] = defaultdict(list)
    for o in outcomes:
        keywords = expected.get(o.case_id)
        if not keywords or not o.judged:
            continue
        for keyword in keywords:
            total += 1
            alternatives = [a for a in keyword.split("|") if a]
            if any(a in o.signal_text for a in alternatives):
                hit += 1
            else:
                misses[o.case_id].append(alternatives[0] if alternatives else keyword)
    return Ratio(count=hit, total=total, detail=dict(misses))


def errors(outcomes: list[Outcome]) -> Counter:
    return Counter(o.error for o in outcomes if not o.judged)
