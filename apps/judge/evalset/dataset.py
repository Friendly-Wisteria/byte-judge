"""評価用テストセットの読み込み。

プロンプトやモデルを変えたときに、良くなったのか悪くなったのかを測るための
入力を持つ。入力内容を保存しない方針のまま改善を回すには、こちらで用意した
固定の入力が要る。

データ本体（cases.toml）はリポジトリに含めない（.gitignore 済み）。
危険側は合成データだが、よくできているほど募集文のテンプレートとして
使えてしまうため、公開する成果物からは外している。書き方は README.md を参照。
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
DEFAULT_PATH = DATA_DIR / "cases.toml"

# カテゴリと、その説明。危険側かどうかもここで決まる。
CATEGORIES = {
    "obvious": "明らかな闇バイト（隠語・高額報酬・秘匿アプリ誘導などが揃っている）",
    "disguised": "一見普通だが危険（表面上は正規求人に見えるが、実態が犯罪加担）",
    "legitimate": "普通の求人（事業者情報が揃っている）",
    "gray": "グレー（判断が難しい、情報が不足している）",
}

# 「危険を伝えるべき」カテゴリ。偽陰性率の分母になる。
DANGEROUS_CATEGORIES = frozenset({"obvious", "disguised"})


class DatasetError(Exception):
    """テストセットを読めない・内容が不正なとき。"""


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    text: str
    # 挙がっていてほしい根拠のキーワード。シグナル理由の妥当性を測るのに使う。
    expect_signals: tuple[str, ...] = ()
    note: str = ""

    @property
    def is_dangerous(self) -> bool:
        return self.category in DANGEROUS_CATEGORIES


def load_cases(path: Path | None = None) -> list[Case]:
    """テストセットを読み込む。壊れていれば DatasetError。

    件数の偏りは弾かない（カテゴリを絞って試すこともあるため）。構成は
    コマンド側で表示して、人が見て気づけるようにしている。
    """
    path = path or DEFAULT_PATH
    if not path.exists():
        raise DatasetError(
            f"テストセットが見つかりません: {path}\n"
            f"リポジトリには含めていません。{DATA_DIR / 'README.md'} を読んで用意してください。"
        )

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise DatasetError(f"TOML として読めません: {path}: {e}") from e

    entries = raw.get("case")
    if not entries:
        raise DatasetError(f"case が1件もありません: {path}")

    cases: list[Case] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        case = _build_case(entry, index, path)
        if case.id in seen:
            raise DatasetError(f"id が重複しています: {case.id}")
        seen.add(case.id)
        cases.append(case)
    return cases


def _build_case(entry: dict, index: int, path: Path) -> Case:
    where = f"{path} の {index} 件目"
    for key in ("id", "category", "text"):
        if not entry.get(key):
            raise DatasetError(f"{where}: {key} がありません")

    category = entry["category"]
    if category not in CATEGORIES:
        raise DatasetError(
            f"{where}: 知らない category です: {category}"
            f"（使えるのは {', '.join(CATEGORIES)}）"
        )

    text = entry["text"].strip()
    if not text:
        raise DatasetError(f"{where}: text が空です")

    return Case(
        id=entry["id"],
        category=category,
        text=text,
        expect_signals=tuple(entry.get("expect_signals", ())),
        note=entry.get("note", ""),
    )


def composition(cases: list[Case]) -> dict[str, int]:
    """カテゴリごとの件数。実行前に構成を見せるために使う。"""
    return {name: sum(1 for c in cases if c.category == name) for name in CATEGORIES}
