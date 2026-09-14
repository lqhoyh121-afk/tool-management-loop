"""Read-only workbook sniffing and raw cell preview. No business mapping, no writes."""
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET
import zipfile


MAX_PREVIEW_ROWS = 50
OLE_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
ZIP_MAGIC = b'PK'
SSML = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
PKGREL = '{http://schemas.openxmlformats.org/package/2006/relationships}'
ODREL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


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
    data = target.read_bytes()
    kind = detect_kind(data)
    if kind == 'empty':
        raise PreviewError('文件是空的，没有可预览的工作表。')
    if kind == 'html_table':
        sheets = _preview_html(data)
        if not sheets:
            raise PreviewError('已识别为 HTML 表格文件，但没有可读取的表格。')
        return WorkbookPreview(kind=kind, sheets=sheets, source_name=target.name)
    if kind == 'xlsx':
        sheets = _preview_xlsx(target)
        if not sheets:
            raise PreviewError('已识别为 Excel 工作簿，但没有可读取的工作表。')
        return WorkbookPreview(kind=kind, sheets=sheets, source_name=target.name)
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
        return 'xlsx' if _looks_like_xlsx(data) else 'unsupported'
    if _looks_like_html(data):
        return 'html_table'
    return 'unsupported'


def _looks_like_xlsx(data):
    try:
        from io import BytesIO
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile:
        return False
    return 'xl/workbook.xml' in names or '[Content_Types].xml' in names


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


class _TableCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self._table = None
        self._row = None
        self._cell = None
        self._parts = []

    def handle_starttag(self, tag, attrs):
        name = tag.lower()
        if name == 'table':
            self._table = []
        elif name == 'tr' and self._table is not None:
            self._row = []
        elif name in {'td', 'th'} and self._row is not None:
            self._cell = True
            self._parts = []

    def handle_endtag(self, tag):
        name = tag.lower()
        if name in {'td', 'th'} and self._cell:
            self._row.append(''.join(self._parts).strip())
            self._cell = False
            self._parts = []
        elif name == 'tr' and self._row is not None:
            if any(cell != '' for cell in self._row):
                self._table.append(self._row)
            self._row = None
        elif name == 'table' and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None

    def handle_data(self, data):
        if self._cell:
            self._parts.append(data)


def _preview_html(data):
    parser = _TableCollector()
    try:
        parser.feed(_decode_text(data))
        parser.close()
    except Exception as exc:
        raise PreviewError(f'HTML 表格解析失败：{exc}') from exc
    sheets = []
    for index, table in enumerate(parser.tables, start=1):
        sheets.append(_sheet_from_rows(f'table-{index}', table))
    return sheets


def _sheet_from_rows(name, rows):
    truncated = len(rows) > MAX_PREVIEW_ROWS
    visible = rows[:MAX_PREVIEW_ROWS]
    headers = list(visible[0]) if visible else []
    body = [list(row) for row in visible[1:]] if len(visible) > 1 else []
    width = max((len(row) for row in visible), default=0)
    headers = _pad(headers, width)
    body = [_pad(row, width) for row in body]
    return SheetPreview(name=name, headers=headers, rows=body, truncated=truncated)


def _pad(row, width):
    values = list(row)
    if len(values) < width:
        values.extend([''] * (width - len(values)))
    return values[:width]


def _preview_xlsx(path):
    try:
        with zipfile.ZipFile(path) as archive:
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
                    rows = _xlsx_sheet_rows(archive, 'xl/worksheets/sheet1.xml', shared)
                    return [_sheet_from_rows('Sheet1', rows)]
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
                rows = _xlsx_sheet_rows(archive, member, shared)
                sheets.append(_sheet_from_rows(name, rows))
            return sheets
    except zipfile.BadZipFile as exc:
        raise PreviewError(f'Excel 工作簿无法打开：{exc}') from exc
    except PreviewError:
        raise
    except Exception as exc:
        raise PreviewError(f'Excel 工作簿解析失败：{exc}') from exc


def _read_xml(archive, name):
    if name not in archive.namelist():
        return None
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


def _xlsx_sheet_rows(archive, member, shared):
    root = _read_xml(archive, member)
    if root is None:
        return []
    grid = {}
    max_row = 0
    max_col = 0
    for cell in root.findall(f'.//{SSML}c'):
        ref = cell.attrib.get('r')
        if not ref:
            continue
        row_idx, col_idx = _a1_to_row_col(ref)
        grid[(row_idx, col_idx)] = _cell_text(cell, shared)
        max_row = max(max_row, row_idx)
        max_col = max(max_col, col_idx)
    rows = []
    for row_idx in range(1, max_row + 1):
        rows.append([grid.get((row_idx, col_idx), '') for col_idx in range(1, max_col + 1)])
    while rows and not any(cell != '' for cell in rows[-1]):
        rows.pop()
    return rows


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
        if sheet.truncated:
            lines.append(f'仅预览前 {MAX_PREVIEW_ROWS} 行，其余未读取为业务数据。')
    return '\n'.join(lines)


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
