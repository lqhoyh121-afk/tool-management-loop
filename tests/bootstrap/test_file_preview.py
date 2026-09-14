import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bootstrap.file_preview import (
    MAX_PREVIEW_ROWS,
    OLE_MAGIC,
    PreviewError,
    format_preview,
    preview_workbook,
)
from bootstrap import file_preview as preview_mod
from xlsx_support import write_xlsx, write_xlsx_sparse


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

    def test_html_rowspan_keeps_columns(self):
        path = self.root / 'rowspan.xls'
        path.write_text(
            '<table>'
            '<tr><td>Tool</td><td>Qty</td></tr>'
            '<tr><td rowspan="2">Glove</td><td>1</td></tr>'
            '<tr><td>2</td></tr>'
            '</table>',
            encoding='utf-8',
        )
        preview = preview_workbook(path)
        sheet = preview.sheets[0]
        self.assertEqual(sheet.headers, ['Tool', 'Qty'])
        self.assertEqual(sheet.rows, [['Glove', '1'], ['', '2']])
        self.assertNotEqual(sheet.rows, [['Glove', '1'], ['2', '']])
        self.assertEqual(sheet.merges[0]['rowspan'], 2)
        self.assertIn('不复制原值', preview.warning)

    def test_html_colspan_and_multilevel_header(self):
        path = self.root / 'colspan.xls'
        path.write_text(
            '<table>'
            '<tr><th colspan="2">库存</th></tr>'
            '<tr><th>Tool</th><th>Qty</th></tr>'
            '<tr><td>Glove</td><td>1</td></tr>'
            '</table>',
            encoding='utf-8',
        )
        preview = preview_workbook(path)
        sheet = preview.sheets[0]
        self.assertEqual(sheet.headers, ['库存', ''])
        self.assertEqual(sheet.rows, [['Tool', 'Qty'], ['Glove', '1']])
        self.assertTrue(sheet.merges)

    def test_sparse_far_cell_does_not_expand_dense_rows(self):
        path = self.root / 'sparse.xlsx'
        write_xlsx_sparse(path, 'S', [(10000, 1, 'far')])
        preview = preview_workbook(path)
        sheet = preview.sheets[0]
        self.assertLessEqual(len(sheet.rows) + 1, MAX_PREVIEW_ROWS)
        self.assertTrue(sheet.truncated)
        self.assertNotIn('far', format_preview(preview))
        self.assertIn('未分配稠密矩阵', format_preview(preview))

    def test_over_limit_file_rejected_before_parse(self):
        path = self.root / 'too-big.xls'
        path.write_bytes(b'<table><tr><td>X</td></tr></table>' + b'x' * 64)
        old = preview_mod.MAX_FILE_BYTES
        preview_mod.MAX_FILE_BYTES = 16
        try:
            with self.assertRaises(PreviewError) as ctx:
                preview_workbook(path)
        finally:
            preview_mod.MAX_FILE_BYTES = old
        self.assertIn('文件过大', ctx.exception.message)

    def test_zip_member_over_limit_rejected(self):
        path = self.root / 'member.xlsx'
        write_xlsx(path, 'S', [['A', 'B'], ['C', 'D']])
        old = preview_mod.MAX_ZIP_MEMBER_BYTES
        preview_mod.MAX_ZIP_MEMBER_BYTES = 40
        try:
            with self.assertRaises(PreviewError) as ctx:
                preview_workbook(path)
        finally:
            preview_mod.MAX_ZIP_MEMBER_BYTES = old
        self.assertIn('压缩成员', ctx.exception.message)

    def test_too_many_zip_members_rejected(self):
        path = self.root / 'many.xlsx'
        write_xlsx(path, 'S', [['A']])
        old = preview_mod.MAX_ZIP_MEMBERS
        preview_mod.MAX_ZIP_MEMBERS = 2
        try:
            with self.assertRaises(PreviewError) as ctx:
                preview_workbook(path)
        finally:
            preview_mod.MAX_ZIP_MEMBERS = old
        self.assertIn('压缩成员过多', ctx.exception.message)


if __name__ == '__main__':
    unittest.main()
