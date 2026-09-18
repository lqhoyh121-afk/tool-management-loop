"""Which field attribute each production reader touches, read off the sources.

The doubles materialize read shapes from :data:`t03_live_cells._KINDS`, a hand-written
declaration. Nothing in the declaration keeps it complete by itself: a field the read
side shapes but the declaration forgets lands on pass-through, so the double stays green
while production fails closed (#73). This module walks the production sources with
``ast`` and reports the reader applied to every field attribute, which is what lets
``t03_kinds_guard`` check the declaration against the read side.

Resolution is static and closed. A field argument that is not a field-map attribute, not
a local alias of one and not a parameter resolved by an attribute-passing caller is a
:class:`ReadSiteError` — never a silent skip, because a skipped site is exactly the hole
this scan exists to close.

Module name is unique so T09 file-based discovery does not collide.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

from t03_live_cells import DATETIME, NUMBER, PERSON, SINGLE_SELECT, TEXT

ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = (ROOT / 'integrations',)
CELLS_SOURCE = ROOT / 'integrations' / 'dingtalk' / 'cells.py'
MAP_CLASS_NAMES = ('FieldMap', 'EntryFieldMap', 'ApplicationFieldMap',
                   'ReturnFormFieldMap')

#: Reader function -> the read shape it demands of its field argument.
READER_SHAPES = {
    'read_single_select': SINGLE_SELECT,
    'read_creator': PERSON,
    'read_number': NUMBER,
    'read_datetime': DATETIME,
    'read_text': TEXT,
    'read_text_or_empty': TEXT,
}


class ReadSiteError(Exception):
    """A read site the scan cannot pin to a field-map attribute."""


class Scan(NamedTuple):
    """Read sites of the scanned sources.

    ``sites`` maps a field attribute to the set of shapes it is read with (a field can
    be read strictly in one place and only checked for presence in another).
    ``locations`` maps attribute -> shape -> ``file:line`` of every site.
    """

    sites: dict
    locations: dict
    problems: list


def map_attributes_by_class():
    """``{map class name: frozenset(attribute names)}`` for the production field maps."""
    from dataclasses import fields as dataclass_fields

    from integrations.dingtalk import layout

    return {
        class_name: frozenset(field.name
                              for field in dataclass_fields(getattr(layout, class_name)))
        for class_name in MAP_CLASS_NAMES
    }


def map_attributes():
    """Attribute names of the production field maps: the only readable fields."""
    names = set()
    for attributes in map_attributes_by_class().values():
        names.update(attributes)
    return names


def _sources(roots):
    found = []
    for root in roots:
        path = Path(root)
        if path.is_file():
            found.append(path)
            continue
        found.extend(sorted(path.rglob('*.py')))
    if not found:
        raise ReadSiteError(f'扫描根下没有源码：{[str(r) for r in roots]}')
    return found


def _reader_defs(path=CELLS_SOURCE):
    """``name -> (shape, field parameter name)`` for the readers defined in cells.py."""
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in READER_SHAPES:
            params = [argument.arg for argument in node.args.args]
            if len(params) < 2:
                raise ReadSiteError(f'{path.name}: {node.name} 不是 (cells, field) 形态')
            found[node.name] = (READER_SHAPES[node.name], params[1])
    missing = sorted(set(READER_SHAPES) - set(found))
    if missing:
        raise ReadSiteError(f'{path.name} 缺少读函数：{", ".join(missing)}')
    return found


def _functions(tree, prefix=''):
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualname = f'{prefix}{node.name}'
            yield qualname, node
            yield from _functions(node, qualname + '.')
        elif isinstance(node, ast.ClassDef):
            yield from _functions(node, f'{prefix}{node.name}.')


def _own_nodes(node):
    """Nodes of one scope, without descending into nested function bodies."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield current
        stack.extend(ast.iter_child_nodes(current))


def _calls(node):
    return [sub for sub in _own_nodes(node) if isinstance(sub, ast.Call)]


def _callee_name(call):
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _attribute_name(node, map_attrs):
    if isinstance(node, ast.Attribute) and node.attr in map_attrs:
        return node.attr
    return None


def _argument(call, index, keyword):
    if index < len(call.args):
        return call.args[index]
    for entry in call.keywords:
        if entry.arg == keyword:
            return entry.value
    return None


def _describe(node):
    if node is None:
        return '缺失'
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError):  # pragma: no cover - defensive
        return ast.dump(node)


def _shown(path):
    """A repo-relative location when the source is inside the repo, else its own path."""
    path = Path(path)
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def scan(roots=SCAN_ROOTS, map_attrs=None, readers=None):
    """Read sites of the production sources, with unresolved ones reported loudly."""
    map_attrs = map_attributes() if map_attrs is None else set(map_attrs)
    readers = _reader_defs() if readers is None else dict(readers)
    functions = []
    for path in _sources(roots):
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for qualname, node in _functions(tree):
            functions.append((path, qualname, node,
                              [argument.arg for argument in node.args.args]))

    by_name = {}
    for path, qualname, node, params in functions:
        by_name.setdefault(node.name, []).append((path, qualname, node, params))

    aliases = {}
    for path, qualname, node, params in functions:
        scope = {}
        for sub in _own_nodes(node):
            if not isinstance(sub, ast.Assign) or len(sub.targets) != 1:
                continue
            target = sub.targets[0]
            if not isinstance(target, ast.Name) or target.id in params:
                continue
            attribute = _attribute_name(sub.value, map_attrs)
            if attribute:
                scope[target.id] = attribute
        aliases[(str(path), qualname)] = scope

    param_shapes = {}

    def slots(call):
        """``(shape, argument index, parameter, callee key)`` for a call's field slot.

        The index is in call-argument space: a bound method call omits ``self``, which
        the definition lists as its first parameter.
        """
        name = _callee_name(call)
        if name in readers:
            shape, keyword = readers[name]
            return [(shape, 1, keyword, None)]
        bound = isinstance(call.func, ast.Attribute)
        found = []
        for path, qualname, _node, params in by_name.get(name, ()):
            offset = 1 if bound and params[:1] == ['self'] else 0
            for index, parameter in enumerate(params):
                shape = param_shapes.get((str(path), qualname, parameter))
                if shape and index >= offset:
                    found.append((shape, index - offset, parameter, (str(path), qualname)))
        return found

    changed = True
    while changed:
        changed = False
        for path, qualname, node, params in functions:
            for call in _calls(node):
                for shape, index, parameter, _callee in slots(call):
                    argument = _argument(call, index, parameter)
                    if not isinstance(argument, ast.Name) or argument.id not in params:
                        continue
                    key = (str(path), qualname, argument.id)
                    if param_shapes.get(key) != shape:
                        param_shapes[key] = shape
                        changed = True

    sites, locations, problems = {}, {}, []
    resolved, dynamic = set(), {}
    reader_definitions = {(str(path), qualname) for path, qualname, node, _params
                          in functions if node.name in READER_SHAPES}
    for path, qualname, node, params in functions:
        for call in _calls(node):
            for shape, index, parameter, callee in slots(call):
                argument = _argument(call, index, parameter)
                location = f'{_shown(path)}:{call.lineno}'
                attribute = _attribute_name(argument, map_attrs)
                if attribute is None and isinstance(argument, ast.Name):
                    attribute = aliases[(str(path), qualname)].get(argument.id)
                if attribute:
                    sites.setdefault(attribute, set()).add(shape)
                    locations.setdefault(attribute, {}).setdefault(shape, []).append(location)
                    if callee is not None:
                        resolved.add((callee[0], callee[1], parameter))
                    continue
                if argument is None:
                    continue  # slot defaulted in this call; the definition is the site
                if isinstance(argument, ast.Name) and argument.id in params:
                    dynamic[(str(path), qualname, argument.id)] = (shape, location)
                    continue
                problems.append(
                    f'{location}: 读侧字段无法静态解析为字段映射属性：{_describe(argument)}'
                )

    for key, (shape, location) in sorted(dynamic.items()):
        if key in resolved or (key[0], key[1]) in reader_definitions:
            # A reader's own field parameter is resolved by construction: callers pass
            # the field id, and that call site is where the field is pinned down.
            continue
        problems.append(
            f'{location}: 读侧字段是参数 {key[2]}（{shape}），没有调用方传入字段映射属性'
        )
    return Scan(sites, locations, problems)


def read_sites(roots=SCAN_ROOTS, map_attrs=None, readers=None):
    """Read sites, refusing outright when any of them cannot be resolved."""
    result = scan(roots=roots, map_attrs=map_attrs, readers=readers)
    if result.problems:
        raise ReadSiteError('读侧扫描失败：\n' + '\n'.join(result.problems))
    return result


def reader_sources_outside(roots=SCAN_ROOTS):
    """Tracked production files using a reader name outside the scanned roots.

    The scan is only worth what it covers: a reader call in a source the scan does not
    read would leave the declaration unchecked there. Test code is not production.
    """
    import re
    import subprocess

    names = set(READER_SHAPES)
    call = re.compile(r'(?<![\w.])(' + '|'.join(sorted(names)) + r')\s*\(')
    covered = {str(path.resolve()) for path in _sources(roots)}
    out = subprocess.check_output(['git', 'ls-files', '*.py'], cwd=ROOT).decode('utf-8')
    outside = []
    for relative in out.splitlines():
        if not relative:
            continue
        path = (ROOT / relative)
        if str(path.resolve()) in covered or relative.startswith('tests/'):
            continue
        if call.search(path.read_text(encoding='utf-8', errors='replace')):
            outside.append(relative)
    return sorted(outside)
