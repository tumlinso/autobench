from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autobench import snapshot


class ScopedFileScanTests(unittest.TestCase):
    def test_prunes_to_root_relative_literal_prefixes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "src/nested").mkdir(parents=True)
            (root / "docs/guide").mkdir(parents=True)
            (root / "evidence/src").mkdir(parents=True)
            (root / ".git/old").mkdir(parents=True)
            (root / ".autobench/state").mkdir(parents=True)
            (root / "src/main.py").write_text("main")
            (root / "src/nested/inner.py").write_text("inner")
            (root / "docs/guide/input.yaml").write_text("input")
            (root / "evidence/src/stale.py").write_text("historical copy")
            (root / ".git/old/ignored.py").write_text("git")
            (root / ".autobench/state/ignored.py").write_text("state")

            walked: list[Path] = []
            real_walk = os.walk

            def recording_walk(path, *args, **kwargs):
                walked.append(Path(path))
                return real_walk(path, *args, **kwargs)

            with patch.object(snapshot.os, "walk", side_effect=recording_walk):
                files = snapshot._files(root, ("src/*.py", "docs/guide/input.yaml"))

            self.assertEqual({path.relative_to(root).as_posix() for path in files},
                             {"src/main.py", "src/nested/inner.py", "docs/guide/input.yaml"})
            self.assertEqual(set(walked), {root / "src"})
            self.assertNotIn(root, walked)

    def test_broad_glob_still_walks_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "registry/nested").mkdir(parents=True)
            (root / "registry/nested/benchmark.toml").write_text("benchmark")
            (root / "benchmark.toml").write_text("root level is not matched by **/*.toml")
            walked: list[Path] = []
            real_walk = os.walk

            def recording_walk(path, *args, **kwargs):
                walked.append(Path(path))
                return real_walk(path, *args, **kwargs)

            with patch.object(snapshot.os, "walk", side_effect=recording_walk):
                files = snapshot._files(root, ("**/*.toml",))

            self.assertEqual([path.relative_to(root).as_posix() for path in files],
                             ["registry/nested/benchmark.toml"])
            self.assertEqual(walked, [root])

    def test_data_walk_is_limited_to_registered_data_prefix(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("fixture-a", "fixture-b", "state"):
                (root / ".autobench" / ("data/" + name if name != "state" else name)).mkdir(parents=True)
            (root / ".autobench/data/fixture-a/input.bin").write_bytes(b"a")
            (root / ".autobench/data/fixture-a/venv/training").mkdir(parents=True)
            (root / ".autobench/data/fixture-a/venv/training/input.bin").write_bytes(b"nested user data")
            (root / ".autobench/data/fixture-b/input.bin").write_bytes(b"b")
            (root / ".autobench/state/input.bin").write_bytes(b"state")
            walked: list[Path] = []
            real_walk = os.walk

            def recording_walk(path, *args, **kwargs):
                walked.append(Path(path))
                return real_walk(path, *args, **kwargs)

            with patch.object(snapshot.os, "walk", side_effect=recording_walk):
                files = snapshot._files(root, (".autobench/data/fixture-a/**",))

            self.assertEqual([path.relative_to(root).as_posix() for path in files],
                             [".autobench/data/fixture-a/input.bin",
                              ".autobench/data/fixture-a/venv/training/input.bin"])
            self.assertEqual(walked, [root / ".autobench/data/fixture-a"])

    def test_symlinked_files_and_prefixes_are_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            outside = Path(temp) / "outside"
            root.mkdir()
            (root / "src").mkdir()
            outside.mkdir()
            (outside / "secret.py").write_text("secret")
            (root / "src/inside.py").write_text("inside")
            try:
                (root / "src/link.py").symlink_to(outside / "secret.py")
                (root / "linked").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks are unavailable")

            self.assertEqual([path.relative_to(root).as_posix()
                              for path in snapshot._files(root, ("src/**", "linked/secret.py"))],
                             ["src/inside.py"])


if __name__ == "__main__":
    unittest.main()
