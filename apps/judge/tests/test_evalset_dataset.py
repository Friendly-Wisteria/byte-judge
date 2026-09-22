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
