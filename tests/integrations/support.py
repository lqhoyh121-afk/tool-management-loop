"""合成夹具加载。测试专用，不进业务模块。"""
import json
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_FILE = Path(__file__).resolve().parent / 'fixtures' / 't01_observed_shapes.json'

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_DOCUMENT = json.loads(FIXTURE_FILE.read_text(encoding='utf-8'))


def fixture_document():
    return deepcopy(_DOCUMENT)


def sample(name):
    """返回一份合成报文的独立副本，改动不影响其他用例。"""
    samples = _DOCUMENT['samples']
    if name not in samples:
        raise KeyError(f'没有名为 {name} 的合成样例')
    return deepcopy(samples[name]['payload'])
