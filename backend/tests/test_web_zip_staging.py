from __future__ import annotations

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile
import unittest

from case_kernel.web_zip_staging import WebZipStagingArea, WebZipStagingBlocked


def _archive(entries: list[tuple[str, bytes]]) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


async def _chunks(value: bytes):
    for start in range(0, len(value), 97):
        yield value[start:start + 97]


class WebZipStagingTests(unittest.IsolatedAsyncioTestCase):
    async def test_inventory_rehashes_every_pdf_and_rejects_unsafe_paths(self) -> None:
        with TemporaryDirectory() as temporary:
            area = WebZipStagingArea(Path(temporary))
            staged = await area.stage_async_chunks(_chunks(_archive([("流水/第一页.pdf", b"pdf-1"), ("第二页.PDF", b"pdf-2")])), client_filename="材料.zip")
            admitted = area.inspect_zip(staged)
            self.assertEqual([entry.name for entry in admitted.entries], ["流水/第一页.pdf", "第二页.PDF"])
            self.assertEqual(admitted.expanded_byte_size, 10)
            area.discard(admitted)
            self.assertFalse(staged.path.exists())

            unsafe = await area.stage_async_chunks(_chunks(_archive([("../escape.pdf", b"x")])), client_filename="危险.zip")
            with self.assertRaises(WebZipStagingBlocked):
                area.inspect_zip(unsafe)
            area.discard(unsafe)

    async def test_rejects_duplicates_non_pdf_and_directory_entries(self) -> None:
        with TemporaryDirectory() as temporary:
            area = WebZipStagingArea(Path(temporary))
            for entries in (
                [("same.pdf", b"a"), ("SAME.PDF", b"b")],
                [("notes.txt", b"not a material")],
                [("folder/", b"")],
            ):
                staged = await area.stage_async_chunks(_chunks(_archive(entries)), client_filename="材料.zip")
                with self.assertRaises(WebZipStagingBlocked):
                    area.inspect_zip(staged)
                area.discard(staged)

