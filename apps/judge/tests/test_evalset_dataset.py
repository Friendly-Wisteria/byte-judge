"""評価用テストセットの読み込みの回帰テスト。

データ本体はリポジトリに含めていないため、壊れた TOML や書き間違いは
実行するまで気づけない。読み込みの時点で落として、原因を出す。
"""

import pathlib
import tempfile

from django.test import SimpleTestCase, override_settings

from ..evalset import dataset as evalset_dataset


@override_settings(VIEW_TEST_MODE=False)
class EvalDatasetTests(SimpleTestCase):
    """評価用テストセットの読み込みの検証。

    データ本体はリポジトリに含めていないため、壊れた TOML や書き間違いは
    実行するまで気づけない。読み込みの時点で落として、原因を出す。
    """

    def _write(self, body):
        directory = tempfile.mkdtemp()
        path = pathlib.Path(directory) / "cases.toml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_a_well_formed_file_is_loaded(self):
        path = self._write(
            """
[[case]]
id = "obvious-01"
category = "obvious"
expect_signals = ["高額報酬"]
text = "日給5万円 即日手渡し"

[[case]]
id = "legit-01"
category = "legitimate"
text = "コンビニスタッフ募集 時給1100円"
"""
        )

        cases = evalset_dataset.load_cases(path)

        self.assertEqual([c.id for c in cases], ["obvious-01", "legit-01"])
        self.assertEqual(cases[0].expect_signals, ("高額報酬",))
        self.assertTrue(cases[0].is_dangerous)
        self.assertFalse(cases[1].is_dangerous)

    def test_duplicate_ids_are_rejected(self):
        path = self._write(
            """
[[case]]
id = "dup"
category = "gray"
text = "a"

[[case]]
id = "dup"
category = "gray"
text = "b"
"""
        )

        with self.assertRaisesMessage(evalset_dataset.DatasetError, "重複"):
            evalset_dataset.load_cases(path)

    def test_an_unknown_category_is_rejected(self):
        path = self._write(
            """
[[case]]
id = "x"
category = "unknown"
text = "a"
"""
        )

        with self.assertRaisesMessage(evalset_dataset.DatasetError, "category"):
            evalset_dataset.load_cases(path)

    def test_empty_text_is_rejected(self):
        path = self._write(
            """
[[case]]
id = "x"
category = "gray"
text = "   "
"""
        )

        with self.assertRaisesMessage(evalset_dataset.DatasetError, "text"):
            evalset_dataset.load_cases(path)

    def test_a_missing_file_explains_where_to_look(self):
        missing = pathlib.Path(tempfile.mkdtemp()) / "nope.toml"

        with self.assertRaisesMessage(evalset_dataset.DatasetError, "README"):
            evalset_dataset.load_cases(missing)

    def test_a_broken_toml_points_at_the_file(self):
        """TOML として壊れている場合、どのファイルかが分かること。"""
        path = self._write('[[case]\nid = "x"\n')

        with self.assertRaisesMessage(evalset_dataset.DatasetError, "TOML"):
            evalset_dataset.load_cases(path)

    def test_a_file_without_any_case_is_rejected(self):
        """読めるが中身が無い場合も、実行に進ませないこと。"""
        path = self._write("# 見出しだけで case が無い\n")

        with self.assertRaisesMessage(
            evalset_dataset.DatasetError, "case が1件もありません"
        ):
            evalset_dataset.load_cases(path)


class EvalCompositionTests(SimpleTestCase):
    """テストセットの構成表示の検証。

    件数の偏りは読み込み側では弾いていない（カテゴリを絞って試すことがある
    ため）。実行前に構成を見せることだけが、偏ったまま測って結論を出すのを
    防いでいる。
    """

    def _cases(self, *categories):
        return [
            evalset_dataset.Case(id=f"c{i}", category=category, text="t")
            for i, category in enumerate(categories, start=1)
        ]

    def test_it_counts_each_category(self):
        composition = evalset_dataset.composition(
            self._cases("obvious", "obvious", "gray")
        )

        self.assertEqual(composition["obvious"], 2)
        self.assertEqual(composition["gray"], 1)

    def test_every_category_is_present_even_with_no_case(self):
        """0件のカテゴリも鍵として返すこと（呼び出し側が有無を判断できる）。"""
        composition = evalset_dataset.composition(self._cases("obvious"))

        self.assertEqual(composition["disguised"], 0)
        self.assertEqual(sorted(composition), sorted(evalset_dataset.CATEGORIES))
