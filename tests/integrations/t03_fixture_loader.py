"""合成夹具加载。测试专用，不进业务模块。

模块名刻意不用通用的 support，避免与其他测试辅助模块撞名。
统一入口按文件导入测试，不会把本目录加入 sys.path；各测试文件须按本文件
所在目录定位后再导入。
"""
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
