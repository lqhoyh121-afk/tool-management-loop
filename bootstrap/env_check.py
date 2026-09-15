"""Inspect the current machine. Never install packages or change system settings."""
from dataclasses import dataclass
from pathlib import Path
import importlib.util
import platform
import sys

from bootstrap.binding import (binding_path, interrupted_path, load_binding,
                               ready_path, require_complete)
from bootstrap.import_confirm import receipt_path
from contracts.model import ContractError


@dataclass(frozen=True)
class CheckItem:
    key: str
    title: str
    status: str
    detail: str


def _python_item(version_info=None):
    info = version_info if version_info is not None else sys.version_info
    major = int(info[0])
    minor = int(info[1])
    micro = int(info[2]) if len(info) > 2 else 0
    version = f'{major}.{minor}.{micro}'
    if (major, minor) >= (3, 11):
        return CheckItem('python', 'Python 运行时', 'pass', f'{version}，满足 3.11+')
    return CheckItem('python', 'Python 运行时', 'fail', f'{version}，需要 3.11 或更高版本')


def _windows_item(system_name=None):
    system = system_name if system_name is not None else platform.system()
    if system == 'Windows':
        return CheckItem('os', '操作系统', 'pass', 'Windows')
    return CheckItem('os', '操作系统', 'fail', f'当前为 {system}，首版目标环境是 Windows')


def _tkinter_item(available=None):
    if available is None:
        available = importlib.util.find_spec('tkinter') is not None
    if available:
        return CheckItem('file_dialog', 'Windows 文件选择', 'pass', 'tkinter 可用，不自动安装依赖')
    return CheckItem(
        'file_dialog',
        'Windows 文件选择',
        'fail',
        'tkinter 不可用。不自动安装，不改系统设置；请使用已带 tkinter 的 Python 3.11+',
    )


def _binding_pair(runtime_dir):
    if runtime_dir is None:
        return None, None, '尚未提供运行目录，未读取绑定'
    runtime = Path(runtime_dir)
    if interrupted_path(runtime).exists() and not binding_path(runtime).exists():
        return None, None, '上次绑定中断，正式配置未写入，不能用 ready 标记恢复'
    try:
        return (*load_binding(runtime_dir), '')
    except (ContractError, OSError, ValueError) as exc:
        return None, None, f'绑定不可用：{exc}'


def _people_item(binding, note):
    if binding is None:
        return CheckItem('people_routing', '人员与路由绑定', 'fail', note or '尚未确认 RuntimeBinding')
    try:
        require_complete(binding)
    except ContractError:
        return CheckItem('people_routing', '人员与路由绑定', 'fail', '绑定不完整，四项核验必须全部为真')
    return CheckItem(
        'people_routing',
        '人员与路由绑定',
        'pass',
        '已确认人员、主账范围与四项核验；不是实时钉钉探测',
    )


def _auth_item(binding, note):
    if binding is None or not binding.account_read_write_verified or not binding.ordinary_ledger_denied:
        return CheckItem(
            'dingtalk_auth',
            '钉钉授权',
            'fail',
            note or '管理账号读写与普通人主账拒绝尚未在绑定中确认为真',
        )
    return CheckItem(
        'dingtalk_auth',
        '钉钉授权',
        'pass',
        '以已确认绑定为准，本向导不做实时 dws 登录探测',
    )


def _ledger_item(runtime_dir, binding):
    if binding is None or runtime_dir is None:
        return CheckItem('ledger_mapping', '台账字段映射与导入', 'fail', '无绑定，不能确认导入')
    if not receipt_path(runtime_dir).is_file():
        return CheckItem(
            'ledger_mapping',
            '台账字段映射与导入',
            'fail',
            '尚无导入回读回执；缺 available/reserved/borrowed/revision 不能编造',
        )
    return CheckItem('ledger_mapping', '台账字段映射与导入', 'pass', '已保存导入回读回执，未写钉钉')


def _ready_item(runtime_dir, binding):
    marker = runtime_dir is not None and ready_path(runtime_dir).exists()
    if binding is None:
        detail = '底层闸门拒绝：绑定未完成'
        if marker:
            detail += '。ready 标记不能绕过'
        return CheckItem('ready_gate', '底层就绪闸门', 'fail', detail)
    try:
        require_complete(binding)
    except ContractError:
        return CheckItem('ready_gate', '底层就绪闸门', 'fail', '绑定不完整，ready 标记不能绕过')
    return CheckItem(
        'ready_gate',
        '底层就绪闸门',
        'pass',
        '闸门检查绑定本身，不读取 ready 文件放行',
    )


def check_environment(*, system_name=None, version_info=None, tkinter_available=None, runtime_dir=None):
    binding, _entry, note = _binding_pair(runtime_dir)
    items = [
        _windows_item(system_name),
        _python_item(version_info),
        _tkinter_item(tkinter_available),
        _auth_item(binding, note),
        _people_item(binding, note),
        _ledger_item(runtime_dir, binding),
        _ready_item(runtime_dir, binding),
    ]
    return items


def has_blocking_failure(items):
    return any(item.status == 'fail' for item in items)


def has_gate_failure(items):
    """Failures that block --require-ready. Import receipt is first-deploy, not the write gate."""
    blocking = {item.key for item in items if item.status == 'fail'}
    return bool(blocking - {'ledger_mapping'})


def format_report(items):
    lines = ['环境检查（不写入钉钉；业务写入走底层闸门，不依赖 BAT 或 ready 文件）']
    for item in items:
        label = {'pass': '通过', 'fail': '未通过', 'pending': '待确认'}[item.status]
        lines.append(f'- {item.title}: {label}。{item.detail}')
    lines.append('未通过项不会被自动修复；ready 标记不能当作已绑定。')
    return '\n'.join(lines)
