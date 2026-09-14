import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bootstrap.file_preview import OLE_MAGIC, PreviewError, format_preview, preview_workbook
from xlsx_support import write_xlsx


class FilePreviewTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()

    def test_html_xls_with_chinese_and_spaces_in_path(self):
        folder = self.root / '临时 目录'
        folder.mkdir()
        path = folder / '台账 预览.xls'
        path.write_text(
            '<html xmlns:o="urn:schemas-microsoft-com:office:office"><table>'
            '<tr><td>名称</td><td>余量</td></tr>'
            '<tr><td>绝缘手套</td><td>1</td></tr>'
            '</table></html>',
            encoding='utf-8',
        )
        before = (path.stat().st_mtime_ns, path.stat().st_size)
        preview = preview_workbook(path)
        after = (path.stat().st_mtime_ns, path.stat().st_size)
        self.assertEqual(before, after)
        self.assertEqual(preview.kind, 'html_table')
        self.assertEqual(preview.sheets[0].headers, ['名称', '余量'])
        self.assertEqual(preview.sheets[0].rows, [['绝缘手套', '1']])
        text = format_preview(preview)
        self.assertIn('原始表头', text)
        self.assertIn('绝缘手套', text)
        self.assertIn('不是业务字段映射', text)

    def test_xlsx_raw_cells(self):
        path = self.root / '合成账本.xlsx'
        write_xlsx(path, '库存', [['工具', '数量'], ['接地线', '2']])
        preview = preview_workbook(path)
        self.assertEqual(preview.kind, 'xlsx')
        self.assertEqual(preview.sheets[0].name, '库存')
        self.assertEqual(preview.sheets[0].headers, ['工具', '数量'])
        self.assertEqual(preview.sheets[0].rows, [['接地线', '2']])

    def test_empty_file(self):
        path = self.root / 'empty.xls'
        path.write_bytes(b'')
        with self.assertRaises(PreviewError) as ctx:
            preview_workbook(path)
        self.assertIn('空', ctx.exception.message)

    def test_unsupported_format(self):
        path = self.root / 'notes.txt'
        path.write_text('this is not a table', encoding='utf-8')
        with self.assertRaises(PreviewError) as ctx:
            preview_workbook(path)
        self.assertIn('无法识别', ctx.exception.message)

    def test_broken_html_table_file(self):
        path = self.root / 'broken.xls'
        path.write_text('<html><table>', encoding='utf-8')
        with self.assertRaises(PreviewError) as ctx:
            preview_workbook(path)
        self.assertIn('HTML', ctx.exception.message)

    def test_ole_xls_identified_without_invented_fields(self):
        path = self.root / 'legacy.xls'
        path.write_bytes(OLE_MAGIC + b'\x00' * 32)
        preview = preview_workbook(path)
        self.assertEqual(preview.kind, 'xls_ole')
        self.assertEqual(preview.sheets, [])
        self.assertIn('不编造', preview.warning)

    def test_missing_file(self):
        with self.assertRaises(PreviewError):
            preview_workbook(self.root / 'missing.xls')

    def test_does_not_touch_source_on_repeated_preview(self):
        path = self.root / 'once.xls'
        original = '<table><tr><td>A</td></tr></table>'.encode('utf-8')
        path.write_bytes(original)
        preview_workbook(path)
        preview_workbook(path)
        self.assertEqual(original, path.read_bytes())


if __name__ == '__main__':
    unittest.main()
