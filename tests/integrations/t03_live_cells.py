"""Read-side cell shapes both doubles must materialize (live aitable contract).

Live reads do not echo the write payload: singleSelect option **names** come back as
``{id, name}`` (the id belongs to the option, not to the row or to the read), person
cells come back as exactly ``[{corpId, userId}]``, numbers come back as strings. A
double that stores write payloads therefore has to rebuild those shapes on read, or a
fake-only shape (a bare ``'awaiting_approval'`` for a select, an int for a number)
reaches production and production gets bent to accept what the platform never returns.

Which field is which kind is a schema fact, not something the value reveals: a stored
option name and a stored text value are both plain strings. Callers declare the map
with :func:`declared_kinds` instead of hardcoding a couple of field ids; fields with no
declared kind pass through unchanged, so a reader that only accepts observed shapes
fails closed rather than being handed a materialized lie.

Option ids are derived from the synthetic field id and the option name: stable for one
option of one field (as on the platform), and never a live id.

Module name is unique so T09 file-based discovery does not collide.
"""
from copy import deepcopy
import hashlib

SINGLE_SELECT = 'singleSelect'
PERSON = 'person'
NUMBER = 'number'

_KINDS = (
    (SINGLE_SELECT, ('state', 'tracked', 'decision')),
    (PERSON, ('borrower', 'approver', 'manager')),
    (NUMBER, ('quantity', 'available', 'reserved', 'borrowed')),
)


def declared_kinds(*field_maps):
    """``{field_id: kind}`` for the attributes the given field maps declare.

    ``None`` maps are skipped so callers can pass optional layouts.
    """
    kinds = {}
    for fields in field_maps:
        if fields is None:
            continue
        for kind, attributes in _KINDS:
            for attribute in attributes:
                field_id = getattr(fields, attribute, None)
                if isinstance(field_id, str) and field_id:
                    kinds[field_id] = kind
    return kinds


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
