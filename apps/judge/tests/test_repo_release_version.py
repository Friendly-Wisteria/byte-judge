"""
pyproject.toml上のバージョンが、リリースタグと整合するかを検証する

# ローカルでの検証方法
```bash
# 変数なし → skip されること（緑ではなく skip と表示されるか）
uv run python manage.py test apps.judge.tests.test_repo_release_version --verbosity 2

# 一致 → pass
RELEASE_TAG=v0.1.0 uv run python manage.py test apps.judge.tests.test_repo_release_version

# 不一致 → 必ず fail すること
RELEASE_TAG=v9.9.9 uv run python manage.py test apps.judge.tests.test_repo_release_version

uv run ruff check .
```

"""

import os
import tomllib
from typing import Any

from django.conf import settings
from django.test import SimpleTestCase


class ReleaseVersionTests(SimpleTestCase):
    """リリースタグと pyproject.toml の版数がズレていないことの検証。

    版数の更新は手作業なので、タグを打つときに pyproject.toml の version を
    上げ忘れても、どこも赤くならないまま公開されてしまう。セルフホストする
    運用者はタグを見て「上げるべきか」を判断するため（CONTRIBUTING.md の
    「リリースとタグの方針」）、ここがズレると判断材料そのものが狂う。

    比較できるのはタグが存在する瞬間だけなので、通常の PR や main への push では
    走らない。CI はタグの push のときだけ RELEASE_TAG を渡している
    （.github/workflows/tests.yml）。ズレに気づくのはタグを打った後になるが、
    その場合はタグを消して打ち直す。
    """

    def _read_pyproject(self):
        with open(settings.BASE_DIR / "pyproject.toml", "rb") as toml_file:
            toml_data: dict[str, Any] = tomllib.load(toml_file)
        return toml_data

    def test_the_release_tag_matches_the_pyproject_version(self):
        """リリースタグのバージョンが、pyproject.toml上のバージョンと一致している。

        *このテストは、環境変数として"RELEASE_TAG"が設定されている時のみ有効
        """
        tag_ver = os.environ.get("RELEASE_TAG")
        # ワークフローはタグ以外のとき空文字を渡すため、未設定と空文字の両方を弾く
        if not tag_ver:
            self.skipTest(reason="RELEASE_TAG が無いので、比較するタグが存在しない")

        # pyproject上のversionは、頭に"v"がないので、tag_verの書式に揃える
        toml_data = self._read_pyproject()
        pyproject_ver = "v" + toml_data["project"]["version"]

        self.assertEqual(tag_ver, pyproject_ver)
