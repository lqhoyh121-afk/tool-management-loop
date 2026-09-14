"""Build a minimal xlsx in a temporary directory. Not a tracked ledger."""
from xml.sax.saxutils import escape
import zipfile

CONTENT_TYPES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>
'''

ROOT_RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
'''


def write_xlsx(path, sheet_name, rows):
    strings = []
    index = {}
    for row in rows:
        for value in row:
            if value not in index:
                index[value] = len(strings)
                strings.append(value)
    sst = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(strings)}" uniqueCount="{len(strings)}">',
    ]
    for value in strings:
        sst.append(f'<si><t>{escape(value)}</t></si>')
    sst.append('</sst>')
    sheet_rows = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>']
    sheet_rows.append('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">')
    sheet_rows.append('<sheetData>')
    for ridx, row in enumerate(rows, start=1):
        sheet_rows.append(f'<row r="{ridx}">')
        for cidx, value in enumerate(row):
            ref = _col(cidx + 1) + str(ridx)
            sheet_rows.append(f'<c r="{ref}" t="s"><v>{index[value]}</v></c>')
        sheet_rows.append('</row>')
    sheet_rows.append('</sheetData></worksheet>')
    workbook = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="{escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets>
</workbook>
'''
    rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>
</Relationships>
'''
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('[Content_Types].xml', CONTENT_TYPES)
        archive.writestr('_rels/.rels', ROOT_RELS)
        archive.writestr('xl/workbook.xml', workbook)
        archive.writestr('xl/_rels/workbook.xml.rels', rels)
        archive.writestr('xl/worksheets/sheet1.xml', ''.join(sheet_rows))
        archive.writestr('xl/sharedStrings.xml', ''.join(sst))


def _col(index):
    text = ''
    remaining = index
    while remaining:
        remaining, rem = divmod(remaining - 1, 26)
        text = chr(65 + rem) + text
    return text
