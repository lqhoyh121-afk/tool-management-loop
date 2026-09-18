"""Checks that keep the read-shape declaration honest.

:data:`t03_live_cells._KINDS` is hand-written, and nothing inside it keeps it complete:
the doubles materialize what it says and pass everything else through. Three checks tie
it to the things it can silently disagree with (#73):

1. **the field maps** — every attribute of every production field map is declared, and
   nothing else is, so a new field cannot inherit pass-through by being forgotten;
2. **the read side** — every read site found by ``t03_read_sites`` is declared, and the
   reader at that site can consume what the declaration puts on the wire;
3. **the platform** — the declaration must agree with the live field types observed
   read-only in :file:`fixtures/t73_live_field_types.json`, so a field the platform
   serves as ``singleSelect`` is never declared or read as text.

:func:`check` returns the problems it found (tests assert on them); :func:`verify`
raises :class:`KindsGuardError`.

Module name is unique so T09 file-based discovery does not collide.
"""
from __future__ import annotations

import json
from pathlib import Path

from t03_live_cells import (DATETIME, KINDS_DECLARATION, NUMBER, PERSON,
                            SINGLE_SELECT, TEXT)
from t03_read_sites import map_attributes, map_attributes_by_class, read_sites

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 't73_live_field_types.json'

#: Live field types each read shape can consume. ``user`` and ``creator`` cells both
#: read back as ``[{corpId, userId}]``; ``number``, ``date`` and ``createdTime`` cells
#: read back as strings.
SHAPE_ACCEPTS = {
    SINGLE_SELECT: frozenset({'singleSelect'}),
    PERSON: frozenset({'user', 'creator'}),
    NUMBER: frozenset({'text', 'number'}),
    DATETIME: frozenset({'date', 'createdTime'}),
    TEXT: frozenset({'text', 'number', 'date', 'createdTime'}),
}

#: Which readers can consume the cells a declared shape puts on the wire: a declared
#: select reads back as an object (only ``read_single_select`` takes that), a declared
#: number reads back as a string.
READERS_FOR = {
    SINGLE_SELECT: frozenset({SINGLE_SELECT}),
    PERSON: frozenset({PERSON}),
    NUMBER: frozenset({NUMBER, TEXT}),
    DATETIME: frozenset({DATETIME, TEXT}),
    TEXT: frozenset({TEXT, NUMBER, DATETIME}),
}


class KindsGuardError(Exception):
    """The declaration disagrees with the read side or with the observed live types."""


def live_type_fixture(path=FIXTURE):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def observed_live_types(fixture=None):
    """``{attribute: frozenset(live types)}``, unioned over the observed tables."""
    data = live_type_fixture() if fixture is None else fixture
    union = {}
    for mapping in (data.get('tables') or {}).values():
        for attribute, live_type in mapping.items():
            union.setdefault(attribute, set()).add(live_type)
    return {attribute: frozenset(types) for attribute, types in union.items()}


def declared_shapes(declaration):
    """``({attribute: shape}, problems)`` for a declaration, refusing duplicates."""
    declared, problems = {}, []
    for shape, attributes in declaration:
        if shape not in SHAPE_ACCEPTS:
            problems.append(f'声明了未知读形态：{shape!r}')
            continue
        for attribute in attributes:
            if attribute in declared:
                problems.append(
                    f'属性 {attribute} 被重复声明：{declared[attribute]} 与 {shape}'
                )
                continue
            declared[attribute] = shape
    return declared, problems


def check(declaration=None, map_attrs=None, observed=None, sites=None, fixture=None):
    """Every disagreement between the declaration, the read side and the platform."""
    declaration = KINDS_DECLARATION if declaration is None else declaration
    map_attrs = map_attributes() if map_attrs is None else set(map_attrs)
    fixture = live_type_fixture() if fixture is None else fixture
    observed = observed_live_types(fixture) if observed is None else observed
    sites = read_sites().sites if sites is None else sites

    found = []
    declared, declaration_problems = declared_shapes(declaration)
    found.extend(declaration_problems)

    for attribute in sorted(map_attrs - set(declared)):
        found.append(
            f'字段映射属性没有声明读形态：{attribute}（新增字段必须显式声明，不能默认原样透传）'
        )
    for attribute in sorted(set(declared) - map_attrs):
        found.append(f'声明了不是字段映射属性的名字：{attribute}（属性名写错，或字段已删除）')

    for attribute, shapes in sorted(sites.items()):
        if attribute not in declared:
            found.append(f'读侧用到但没有声明的字段：{attribute}')
            continue
        live = observed.get(attribute)
        for shape in sorted(shapes):
            if live and live - SHAPE_ACCEPTS[shape]:
                found.append(
                    f'{attribute} 被按 {shape} 读，但真机字段类型是 {sorted(live)}'
                )
            if shape not in READERS_FOR[declared[attribute]]:
                found.append(
                    f'{attribute} 声明为 {declared[attribute]}，读侧却按 {shape} 读'
                )

    for attribute, shape in sorted(declared.items()):
        live = observed.get(attribute)
        if not live:
            found.append(
                f'没有真机字段类型观测就声明了读形态：{attribute}（先做只读 field get 观测）'
            )
            continue
        unknown = live - SHAPE_ACCEPTS[shape]
        if unknown:
            found.append(f'{attribute} 声明为 {shape}，但真机字段类型是 {sorted(unknown)}')

    tables = fixture.get('tables') or {}
    gaps = fixture.get('unobserved') or {}
    for table, missing in sorted(gaps.items()):
        if table not in tables:
            found.append(f'观测夹具记了未观测表 {table}，但该表没有已观测字段')
        for attribute in sorted(missing):
            if attribute not in declared:
                found.append(f'未观测注记提到没有声明的属性：{table}/{attribute}')
            if attribute in tables.get(table, {}):
                found.append(f'{table}/{attribute} 同时记为已观测和未观测')

    by_class = map_attributes_by_class()
    for class_name, table_names in sorted((fixture.get('_tables_for_map') or {}).items()):
        if class_name not in by_class:
            found.append(f'观测夹具给不是字段映射的类记了表：{class_name}')
            continue
        known = set()
        for table in table_names:
            if table not in tables:
                found.append(f'{class_name} 指向的表 {table} 没有观测记录')
                continue
            known.update(tables[table])
            known.update(gaps.get(table, {}))
        for attribute in sorted(by_class[class_name] - known):
            found.append(
                f'{class_name} 的字段 {attribute} 在 {table_names} 上既没有观测，'
                '也没有「真机类型未观测」注记'
            )
    return found


def verify(**kwargs):
    found = check(**kwargs)
    if found:
        raise KindsGuardError('读形态声明与读侧/真机观测不一致：\n' + '\n'.join(found))
    return True
