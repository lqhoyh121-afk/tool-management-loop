"""Read-only workbook sniffing and raw cell preview. No business mapping, no writes."""
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET
import zipfile


MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_SNIFF_BYTES = 8192
MAX_ZIP_MEMBERS = 80
MAX_ZIP_MEMBER_BYTES = 2 * 1024 * 1024
MAX_ZIP_TOTAL_UNCOMPRESSED = 8 * 1024 * 1024
MAX_PREVIEW_ROWS = 50
MAX_PREVIEW_COLS = 32
MAX_HTML_TABLES = 8
MAX_HTML_CELLS = MAX_PREVIEW_ROWS * MAX_PREVIEW_COLS
MAX_HTML_TEXT_CHARS = 64 * 1024
MAX_EXCEL_ROW = 1_048_576
MAX_EXCEL_COL = 16_384
OLE_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
ZIP_MAGIC = b'PK'
SSML = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
PKGREL = '{http://schemas.openxmlformats.org/package/2006/relationships}'
ODREL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
MERGED_COVER = ''


class PreviewError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


@dataclass
class SheetPreview:
    name: str
    headers: list
    rows: list
    truncated: bool = False
    merges: list = field(default_factory=list)


@dataclass
class WorkbookPreview:
    kind: str
    sheets: list = field(default_factory=list)
    warning: str = ''
    source_name: str = ''


def preview_workbook(path):
    target = Path(path)
    if not target.exists() or not target.is_file():
        raise PreviewError('未找到所选文件，或选择已取消。')
    size = target.stat().st_size
    if size == 0:
        raise PreviewError('文件是空的，没有可预览的工作表。')
    if size > MAX_FILE_BYTES:
        raise PreviewError(f'文件过大（{size} 字节），超过预览上限 {MAX_FILE_BYTES} 字节，未解析。')
    with target.open('rb') as handle:
        head = handle.read(MAX_SNIFF_BYTES)
    kind = detect_kind(head)
    if kind == 'empty':
        raise PreviewError('文件是空的，没有可预览的工作表。')
    if kind == 'html_table':
        data = target.read_bytes()
        sheets = _preview_html(data)
        if not sheets:
            raise PreviewError('已识别为 HTML 表格文件，但没有可读取的表格。')
        warning = _merge_warning(sheets)
        return WorkbookPreview(kind=kind, sheets=sheets, source_name=target.name, warning=warning)
    if kind == 'xlsx':
        sheets = _preview_xlsx(target)
        if not sheets:
            raise PreviewError('已识别为 Excel 工作簿，但没有可读取的工作表。')
        warning = _merge_warning(sheets)
        return WorkbookPreview(kind=kind, sheets=sheets, source_name=target.name, warning=warning)
    if kind == 'xls_ole':
        return WorkbookPreview(
            kind=kind,
            sheets=[],
            source_name=target.name,
            warning='已识别为二进制 Excel（.xls）。单元格预览需要主控确认的第三方库，本阶段不私加依赖，也不编造表头或业务字段。',
        )
    raise PreviewError('无法识别为 Excel 或 HTML 内容的 xls。未修改源文件，未生成业务字段。')


def detect_kind(data):
    if not data or not data.strip():
        return 'empty'
    if data.startswith(OLE_MAGIC):
        return 'xls_ole'
    if data.startswith(ZIP_MAGIC):
        return 'xlsx'
    if _looks_like_html(data):
        return 'html_table'
    return 'unsupported'


def _looks_like_html(data):
    text = _decode_text(data).lstrip().lower()
    if text.startswith('<!doctype html') or text.startswith('<html') or text.startswith('<table'):
        return True
    markers = (
        'xmlns:x="urn:schemas-microsoft-com:office:excel"',
        'xmlns:o="urn:schemas-microsoft-com:office:office"',
        '<table',
    )
    return any(marker in text[:4000] for marker in markers)


def _decode_text(data):
    if data.startswith(b'\xff\xfe'):
        return data.decode('utf-16-le', errors='replace')
    if data.startswith(b'\xfe\xff'):
        return data.decode('utf-16-be', errors='replace')
    if data.startswith(b'\xef\xbb\xbf'):
        return data.decode('utf-8-sig', errors='replace')
    for encoding in ('utf-8', 'gb18030'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace')


def _span_value(attrs, name):
    raw = dict(attrs).get(name, '1')
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    if value < 1:
        return 1
    return value


class _TableCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self._grid = None
        self._occupied = None
        self._merges = None
        self._row = 0
        self._col = 1
        self._pending = None
        self._parts = []
        self._truncated = False
        self._cells_used = 0
        self._text_used = 0
        self._stopped = False

    def handle_starttag(self, tag, attrs):
        name = tag.lower()
        if name == 'table':
            if self._stopped:
                return
            if len(self.tables) >= MAX_HTML_TABLES:
                self._stopped = True
                raise PreviewError(
                    f'HTML 表格数量超过预览上限 {MAX_HTML_TABLES}，已停止解析，未继续展开。'
                )
            self._grid = {}
            self._occupied = set()
            self._merges = []
            self._row = 0
            self._truncated = False
        elif name == 'tr' and self._grid is not None:
            self._row += 1
            self._col = 1
        elif name in {'td', 'th'} and self._grid is not None and self._row:
            rowspan = _span_value(attrs, 'rowspan')
            colspan = _span_value(attrs, 'colspan')
            while (self._row, self._col) in self._occupied:
                self._col += 1
            self._pending = (self._row, self._col, rowspan, colspan)
            self._parts = []

    def handle_endtag(self, tag):
        name = tag.lower()
        if name in {'td', 'th'} and self._pending is not None:
            row, col, rowspan, colspan = self._pending
            text = ''.join(self._parts).strip()
            self._place_cell(row, col, rowspan, colspan, text)
            self._col = col + min(colspan, MAX_PREVIEW_COLS)
            self._pending = None
            self._parts = []
        elif name == 'table' and self._grid is not None:
            self.tables.append({
                'grid': self._grid,
                'merges': self._merges,
                'truncated': self._truncated,
            })
            self._grid = None
            self._occupied = None
            self._merges = None

    def handle_data(self, data):
        if self._pending is not None:
            self._parts.append(data)

    def _place_cell(self, row, col, rowspan, colspan, text):
        if row > MAX_EXCEL_ROW or col > MAX_EXCEL_COL:
            raise PreviewError(f'HTML 表格坐标超出上限：row={row}, col={col}。')
        if row > MAX_PREVIEW_ROWS or col > MAX_PREVIEW_COLS:
            self._truncated = True
            return
        row_span = min(rowspan, MAX_PREVIEW_ROWS - row + 1)
        col_span = min(colspan, MAX_PREVIEW_COLS - col + 1)
        if rowspan > row_span or colspan > col_span:
            self._truncated = True
        claim = row_span * col_span
        if self._cells_used + claim > MAX_HTML_CELLS:
            self._stopped = True
            raise PreviewError(
                f'HTML 累计预览单元格将超过上限 {MAX_HTML_CELLS}，已停止解析，未继续展开合并格。'
            )
        if self._text_used + len(text) > MAX_HTML_TEXT_CHARS:
            self._stopped = True
            raise PreviewError(
                f'HTML 累计文本将超过上限 {MAX_HTML_TEXT_CHARS} 字符，已停止解析。'
            )
        self._cells_used += claim
        self._text_used += len(text)
        self._grid[(row, col)] = text
        if row_span > 1 or col_span > 1:
            self._merges.append({'row': row, 'col': col, 'rowspan': rowspan, 'colspan': colspan, 'text': text})
        for rr in range(row, row + row_span):
            for cc in range(col, col + col_span):
                self._occupied.add((rr, cc))
                if (rr, cc) != (row, col):
                    self._grid.setdefault((rr, cc), MERGED_COVER)


def _preview_html(data):
    parser = _TableCollector()
    try:
        parser.feed(_decode_text(data))
        parser.close()
    except PreviewError:
        raise
    except Exception as exc:
        raise PreviewError(f'HTML 表格解析失败：{exc}') from exc
    sheets = []
    for index, table in enumerate(parser.tables, start=1):
        sheets.append(_sheet_from_grid(f'table-{index}', table['grid'], table['merges'], table['truncated']))
    return [sheet for sheet in sheets if sheet.headers or sheet.rows or sheet.merges]


def _sheet_from_grid(name, grid, merges, truncated):
    if not grid:
        return SheetPreview(name=name, headers=[], rows=[], truncated=truncated, merges=merges)
    max_row = min(max(row for row, _col in grid), MAX_PREVIEW_ROWS)
    max_col = min(max(col for _row, col in grid), MAX_PREVIEW_COLS)
    if any(row > MAX_PREVIEW_ROWS or col > MAX_PREVIEW_COLS for row, col in grid):
        truncated = True
    matrix = []
    for row in range(1, max_row + 1):
        matrix.append([grid.get((row, col), '') for col in range(1, max_col + 1)])
    headers = list(matrix[0]) if matrix else []
    body = [list(row) for row in matrix[1:]]
    return SheetPreview(name=name, headers=headers, rows=body, truncated=truncated, merges=list(merges))


def _preview_xlsx(path):
    try:
        with zipfile.ZipFile(path) as archive:
            _assert_zip_budget(archive)
            if 'xl/workbook.xml' not in archive.namelist() and '[Content_Types].xml' not in archive.namelist():
                raise PreviewError('ZIP 容器不是可识别的 Excel 工作簿。')
            workbook = _read_xml(archive, 'xl/workbook.xml')
            rels = _read_xml(archive, 'xl/_rels/workbook.xml.rels')
            shared = _shared_strings(archive)
            rel_map = {}
            if rels is not None:
                for rel in rels.findall(f'{PKGREL}Relationship'):
                    rel_map[rel.attrib.get('Id')] = rel.attrib.get('Target', '')
            sheets = []
            sheet_nodes = []
            if workbook is not None:
                sheet_nodes = workbook.findall(f'.//{SSML}sheet')
            if not sheet_nodes:
                if 'xl/worksheets/sheet1.xml' in archive.namelist():
                    return [_xlsx_sheet_preview(archive, 'Sheet1', 'xl/worksheets/sheet1.xml', shared)]
                return []
            for node in sheet_nodes:
                name = node.attrib.get('name') or 'Sheet'
                rel_id = node.attrib.get(f'{ODREL}id') or node.attrib.get('id')
                target = rel_map.get(rel_id, '')
                if not target:
                    continue
                member = target if target.startswith('xl/') else 'xl/' + target.lstrip('/')
                if member.startswith('xl/xl/'):
                    member = member[3:]
                sheets.append(_xlsx_sheet_preview(archive, name, member, shared))
            return sheets
    except zipfile.BadZipFile as exc:
        raise PreviewError(f'Excel 工作簿无法打开：{exc}') from exc
    except PreviewError:
        raise
    except Exception as exc:
        raise PreviewError(f'Excel 工作簿解析失败：{exc}') from exc


def _assert_zip_budget(archive):
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_MEMBERS:
        raise PreviewError(f'压缩成员过多（{len(infos)}），超过预览上限 {MAX_ZIP_MEMBERS}。')
    total = 0
    for info in infos:
        if info.file_size > MAX_ZIP_MEMBER_BYTES:
            raise PreviewError(
                f'压缩成员 {info.filename} 声明解压后 {info.file_size} 字节，超过预览上限 {MAX_ZIP_MEMBER_BYTES} 字节。'
            )
        total += info.file_size
        if total > MAX_ZIP_TOTAL_UNCOMPRESSED:
            raise PreviewError(f'ZIP 声明解压总量 {total} 字节，超过预览上限 {MAX_ZIP_TOTAL_UNCOMPRESSED} 字节。')


def _read_xml(archive, name):
    if name not in archive.namelist():
        return None
    info = archive.getinfo(name)
    if info.file_size > MAX_ZIP_MEMBER_BYTES:
        raise PreviewError(f'压缩成员 {name} 过大，未展开。')
    return ET.fromstring(archive.read(name))


def _shared_strings(archive):
    root = _read_xml(archive, 'xl/sharedStrings.xml')
    if root is None:
        return []
    values = []
    for si in root.findall(f'{SSML}si'):
        texts = [node.text or '' for node in si.iter(f'{SSML}t')]
        values.append(''.join(texts))
    return values


def _xlsx_sheet_preview(archive, name, member, shared):
    root = _read_xml(archive, member)
    if root is None:
        return SheetPreview(name=name, headers=[], rows=[])
    grid = {}
    truncated = False
    for cell in root.findall(f'.//{SSML}c'):
        ref = cell.attrib.get('r')
        if not ref:
            continue
        row_idx, col_idx = _a1_to_row_col(ref)
        if row_idx > MAX_EXCEL_ROW or col_idx > MAX_EXCEL_COL:
            raise PreviewError(f'单元格坐标超出上限：{ref}。')
        if row_idx > MAX_PREVIEW_ROWS or col_idx > MAX_PREVIEW_COLS:
            truncated = True
            continue
        grid[(row_idx, col_idx)] = _cell_text(cell, shared)
    return _sheet_from_grid(name, grid, [], truncated)


def _cell_text(cell, shared):
    kind = cell.attrib.get('t')
    if kind == 's':
        value = cell.findtext(f'{SSML}v') or '0'
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return value
    if kind == 'inlineStr':
        return ''.join(node.text or '' for node in cell.iter(f'{SSML}t'))
    value = cell.findtext(f'{SSML}v')
    return value if value is not None else ''


def _a1_to_row_col(ref):
    letters = ''
    digits = ''
    for char in ref:
        if char.isalpha():
            letters += char.upper()
        elif char.isdigit():
            digits += char
    col = 0
    for char in letters:
        col = col * 26 + (ord(char) - 64)
    return int(digits or '1'), col


def _merge_warning(sheets):
    if any(sheet.merges for sheet in sheets):
        return '表格含合并单元格，已按行列坐标展开；被合并覆盖的格子留空，不复制原值，也不猜填业务字段。'
    if any(sheet.truncated for sheet in sheets):
        return f'仅保留预览窗口内的单元格（最多 {MAX_PREVIEW_ROWS} 行 × {MAX_PREVIEW_COLS} 列），窗外坐标未展开为稠密表。'
    return ''


def format_preview(preview):
    lines = [
        f'文件: {preview.source_name}',
        f'识别结果: {_kind_label(preview.kind)}',
        '源文件未修改。以下是原始表头和原始单元格，不是业务字段映射。',
    ]
    if preview.warning:
        lines.append(preview.warning)
    if not preview.sheets:
        lines.append('没有可展示的工作表单元格。')
        return '\n'.join(lines)
    for sheet in preview.sheets:
        lines.append(f'工作表: {sheet.name}')
        lines.append('原始表头: ' + _join_cells(sheet.headers))
        if not sheet.rows:
            lines.append('原始单元格: （仅有表头行）')
        else:
            lines.append('原始单元格:')
            for row in sheet.rows:
                lines.append('  ' + _join_cells(row))
        if sheet.merges:
            lines.append('合并区: ' + '; '.join(_format_merge(item) for item in sheet.merges))
        if sheet.truncated:
            lines.append(
                f'仅预览窗口内 {MAX_PREVIEW_ROWS} 行 × {MAX_PREVIEW_COLS} 列；窗外或超限内容未分配稠密矩阵。'
            )
    return '\n'.join(lines)


def _format_merge(item):
    return f"r{item['row']}c{item['col']} {item['rowspan']}x{item['colspan']}"


def _kind_label(kind):
    return {
        'html_table': 'HTML 内容的表格文件（含伪装 xls）',
        'xlsx': 'Excel 工作簿（xlsx/xlsm 容器）',
        'xls_ole': '二进制 Excel（OLE .xls）',
    }.get(kind, kind)


def _join_cells(row):
    if not row:
        return '（空）'
    return ' | '.join(cell.replace('\n', ' ') for cell in row)
