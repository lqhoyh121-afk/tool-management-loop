"""Inspect the current machine. Never install packages or change system settings."""
from dataclasses import dataclass
import importlib.util
import platform
import sys


PENDING_T02 = '待确认（等待 T02 冻结公共约定）'


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


def check_environment(*, system_name=None, version_info=None, tkinter_available=None):
    items = [
        _windows_item(system_name),
        _python_item(version_info),
        _tkinter_item(tkinter_available),
        CheckItem('dingtalk_auth', '钉钉授权', 'pending', PENDING_T02),
        CheckItem('people_routing', '人员与路由绑定', 'pending', PENDING_T02),
        CheckItem('ledger_mapping', '台账字段映射与导入', 'pending', PENDING_T02),
        CheckItem('ready_gate', '底层就绪闸门', 'pending', PENDING_T02),
    ]
    return items


def has_blocking_failure(items):
    return any(item.status == 'fail' for item in items)


def format_report(items):
    lines = ['环境检查（本阶段不写入钉钉、不导入台账）']
    for item in items:
        label = {'pass': '通过', 'fail': '未通过', 'pending': '待确认'}[item.status]
        lines.append(f'- {item.title}: {label}。{item.detail}')
    lines.append('未通过项不会被自动修复；待确认项不得报告为已满足。')
    return '\n'.join(lines)
