from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from adapters.common.safe_tar import extract_data_archive


class SafeTarExtractionTests(unittest.TestCase):
    def archive(self, *members: tuple[tarfile.TarInfo, bytes]) -> io.BytesIO:
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for info, content in members:
                archive.addfile(info, io.BytesIO(content) if info.isfile() else None)
        stream.seek(0)
        return stream

    def test_extracts_regular_release_files_on_python_311_and_newer(self):
        info = tarfile.TarInfo("release/manifest.json")
        content = b"{}\n"
        info.size = len(content)

        with tempfile.TemporaryDirectory() as temporary:
            with tarfile.open(fileobj=self.archive((info, content)), mode="r:gz") as archive:
                extract_data_archive(archive, temporary)
            self.assertEqual((Path(temporary) / "release/manifest.json").read_bytes(), content)

    def test_rejects_parent_traversal_and_links(self):
        traversal = tarfile.TarInfo("../outside.txt")
        traversal_content = b"outside"
        traversal.size = len(traversal_content)
        link = tarfile.TarInfo("release/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../outside.txt"

        for member, content in ((traversal, traversal_content), (link, b"")):
            with self.subTest(member=member.name), tempfile.TemporaryDirectory() as temporary:
                with tarfile.open(fileobj=self.archive((member, content)), mode="r:gz") as archive:
                    with self.assertRaises(tarfile.ExtractError):
                        extract_data_archive(archive, temporary)


if __name__ == "__main__":
    unittest.main()
