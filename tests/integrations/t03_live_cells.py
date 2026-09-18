"""Read-side cell shapes both doubles must materialize (live aitable contract).

Live reads do not echo the write payload: singleSelect option **names** come back as
``{id, name}`` (the id belongs to the option, not to the row or to the read), person
cells come back as exactly ``[{corpId, userId}]``, numbers come back as strings. A
double that stores write payloads therefore has to rebuild those shapes on read, or a
fake-only shape (a bare ``'awaiting_approval'`` for a select, an int for a number)
reaches production and production gets bent to accept what the platform never returns.

Which field is which kind is a schema fact, not something the value reveals: a stored
option name and a stored text value are both plain strings. The declaration is
:data:`_KINDS` — every attribute of every field map, with the read shape that field
carries — and it is **checked rather than trusted**: ``t03_kinds_guard`` requires the
declaration to cover the read sites found in the production sources and to agree with
the live field types observed read-only in :file:`fixtures/t73_live_field_types.json`.
A field missing from the declaration is a red test, never a silent pass-through (#73).

These are read shapes, not a whitelist of business fields: fields declared
``singleSelect`` / ``person`` / ``number`` are materialized by the doubles; the rest
(``text``, ``datetime``) pass through unchanged, because the write payload already is
the observed read shape — ``encode_*`` writes a timezone-aware ISO string for dates.
Option ids are derived from the synthetic field id and the option name: stable for one
option of one field (as on the platform), and never a live id.

Module name is unique so T09 file-based discovery does not collide.
"""
from copy import deepcopy
import hashlib

SINGLE_SELECT = 'singleSelect'
PERSON = 'person'
NUMBER = 'number'
TEXT = 'text'
DATETIME = 'datetime'

#: Shapes the doubles rebuild on read; the other declared shapes pass through.
MATERIALIZED = (SINGLE_SELECT, PERSON, NUMBER)

#: Key the file double reads its ``{field_id: shape}`` map from.
KINDS_KEY = 'kinds'


class KindsError(Exception):
    """A field-kind declaration the doubles refuse to guess around."""


#: Every readable field attribute, with the read shape it carries. All attributes of
#: the four production field maps appear here exactly once, so a field added to a map
#: without a declared shape fails ``t03_kinds_guard`` loudly instead of silently
#: passing through.
#:
#: ``item`` is a single-select question under that name on two tables. On the
#: application table it is #89's「工具」question, whose option name the engine resolves to
#: an inventory record (``DingTalkAdapter._item_by_name``); its live type was observed
#: read-only as ``singleSelect``. On the stage-entry table it is #77's optional
#: 「归还物品」question: the read side (``DingTalkAdapter._return_item_value``) takes the
#: ``{id, name}`` object and matches on ``.name`` (a plain string, which the live pre-#77
#: rows and a text field in that slot would produce, is tolerated too), so the declared
#: read shape is ``singleSelect`` — the shape the double must materialize. That table's
#: live type is *not* observed: the question is added by a human on the form view and the
#: table does not carry it yet, which is what the fixture's「真机类型未观测」note records.
_KINDS = (
    (SINGLE_SELECT, ('state', 'tracked', 'decision', 'item')),
    (PERSON, ('borrower', 'approver', 'manager')),
    (NUMBER, ('quantity', 'available', 'reserved', 'borrowed')),
    (DATETIME, ('due_at', 'occurred_at')),
    (TEXT, ('action', 'application_evidence', 'available_ids', 'borrowed_ids',
            'config_version', 'consumed_events', 'item_container', 'item_id',
            'loan_container', 'loan_id', 'operation_id', 'physical_ids',
            'reserved_ids', 'return_container', 'return_id', 'revision')),
)

KINDS_DECLARATION = _KINDS


def declared_kinds(*field_maps):
    """``{field_id: shape}`` for the materialized attributes the given maps declare.

    ``None`` maps are skipped so callers can pass optional layouts. Calling this with no
    map at all is an error: the double would fall back to passing write payloads through
    unchanged, which is the shape #33 removed.
    """
    provided = [fields for fields in field_maps if fields is not None]
    if not provided:
        raise KindsError(
            '没有字段映射就没有可物化的字段类型，替身会退化成原样透传（#33 之前的形态）'
        )
    kinds = {}
    for fields in provided:
        for shape, attributes in _KINDS:
            if shape not in MATERIALIZED:
                continue
            for attribute in attributes:
                field_id = getattr(fields, attribute, None)
                if isinstance(field_id, str) and field_id:
                    kinds[field_id] = shape
    return kinds


def validate_state_kinds(state):
    """The shape map a harness declared in the double's state, or a loud refusal.

    The file double used to read ``state.get('kinds') or {}``: a hand-made state, an old
    backup or another harness silently got the loose pre-#33 double, with payloads
    passed through as if they were live reads. A missing key, a non-mapping, an empty
    field id and an unknown shape name are all refusals now.
    """
    if KINDS_KEY not in state:
        raise KindsError(
            f'state 未声明 {KINDS_KEY}（字段 ID -> 读回形态）；缺它时假 CLI 只能原样透传，'
            '读侧拿不到真机形态'
        )
    declared = state[KINDS_KEY]
    if not isinstance(declared, dict):
        raise KindsError(f'{KINDS_KEY} 应为「字段 ID -> 读回形态」的对象')
    for field_id, shape in declared.items():
        if not isinstance(field_id, str) or not field_id:
            raise KindsError(f'{KINDS_KEY} 出现不是字段 ID 的键：{field_id!r}')
        if shape not in MATERIALIZED:
            raise KindsError(
                f'字段 {field_id} 声明的读回形态 {shape!r} 未知；可用形态：'
                f'{", ".join(MATERIALIZED)}'
            )
    return dict(declared)


def option_id(field_id, name):
    """Synthetic server-side option id, stable for one option of one field."""
    digest = hashlib.sha1(f'{field_id}\x1f{name}'.encode('utf-8')).hexdigest()
    return f'SYNTHETIC-rand-{digest[:12]}'


def live_select(field_id, name):
    return {'id': option_id(field_id, name), 'name': name}


def live_person(value):
    """``[{corpId, userId}]``, or ``None`` when the stored value is not that shape."""
    if not isinstance(value, list) or not value:
        return None
    people = []
    for entry in value:
        if not isinstance(entry, dict):
            return None
        corp_id = entry.get('corpId')
        user_id = entry.get('userId')
        if not isinstance(corp_id, str) or not corp_id:
            return None
        if not isinstance(user_id, str) or not user_id:
            return None
        people.append({'corpId': corp_id, 'userId': user_id})
    return people


def live_cells(cells, kinds):
    """Cells as the live read returns them; unshaped or undeclared values pass through."""
    visible = {}
    for field_id, value in cells.items():
        if value == '':
            continue
        kind = kinds.get(field_id)
        if kind == SINGLE_SELECT:
            name = value.get('name') if isinstance(value, dict) else value
            if isinstance(name, str) and name:
                visible[field_id] = live_select(field_id, name)
                continue
        elif kind == PERSON:
            people = live_person(value)
            if people is not None:
                visible[field_id] = people
                continue
        elif kind == NUMBER and not isinstance(value, bool):
            if isinstance(value, (int, float, str)) and str(value):
                visible[field_id] = str(value)
                continue
        visible[field_id] = deepcopy(value)
    return visible


def filter_value(kinds, field_id, value):
    """What a server-side ``--filters`` eq compares: the option name for selects."""
    if kinds.get(field_id) == SINGLE_SELECT and isinstance(value, dict):
        return value.get('name')
    return value
