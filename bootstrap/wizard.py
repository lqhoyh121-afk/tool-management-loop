"""Visible terminal wizard. No background workers, no DingTalk writes."""
import argparse
import sys
from pathlib import Path

from bootstrap.binding import document_from_files, load_binding, require_complete, save_binding
from bootstrap.env_check import check_environment, format_report, has_gate_failure
from bootstrap.file_preview import PreviewError, format_preview, preview_workbook
from bootstrap.gate import assert_business_allowed
from bootstrap.import_confirm import confirm_import
from bootstrap.instance import MachineLock
from bootstrap.paths import lock_root, runtime_dir
from contracts.model import ContractError

MENU = """
请选择（预览不写钉钉；导入确认须走绑定与闸门）:
1. 选择文件并预览原始表格
2. 退出
"""


def main(argv=None, *, stdin=None, stdout=None, picker=None, wait_on_error=None, environ_kwargs=None):
    args = argv if argv is not None else sys.argv[1:]
    in_stream = stdin if stdin is not None else sys.stdin
    out_stream = stdout if stdout is not None else sys.stdout
    wait = in_stream.isatty() if wait_on_error is None else wait_on_error
    try:
        return _run(args, in_stream, out_stream, picker, environ_kwargs or {})
    except KeyboardInterrupt:
        _write(out_stream, '已中断。没有后台业务进程需要清理。')
        return 130
    except ContractError as exc:
        _write(out_stream, f'闸门拒绝：{exc.code.value}')
        return 1
    except Exception as exc:
        _write(out_stream, f'启动失败：{exc}')
        if wait:
            _write(out_stream, '按 Enter 关闭窗口。')
            try:
                in_stream.readline()
            except Exception:
                pass
        return 1


def _env_kwargs(parsed, environ_kwargs):
    keys = ('system_name', 'version_info', 'tkinter_available')
    values = {key: environ_kwargs[key] for key in keys if key in environ_kwargs}
    runtime = parsed.runtime or environ_kwargs.get('runtime_dir')
    if runtime:
        values['runtime_dir'] = runtime
    return values


def _run(args, in_stream, out_stream, picker, environ_kwargs):
    parsed = _parse_args(args)
    env_kwargs = _env_kwargs(parsed, environ_kwargs)
    items = check_environment(**env_kwargs)
    _write(out_stream, '工器具借还部署向导（T04 绑定与闸门）')
    _write(out_stream, format_report(items))
    if parsed.check_env:
        return 1 if has_gate_failure(items) else 0
    if parsed.require_ready and has_gate_failure(items):
        _write(out_stream, '环境或绑定未通过，停止后续操作。不会自动安装软件或修改系统。ready 标记不能绕过闸门。')
        return 1
    runtime = env_kwargs.get('runtime_dir') or runtime_dir()
    locks_dir = parsed.lock_root or environ_kwargs.get('lock_root') or lock_root()
    if parsed.bind:
        save_binding(runtime, document_from_files(parsed.bind))
        _write(out_stream, '绑定已保存。未覆盖既有配置，也未把 ready 文件当作绑定。')
        if not parsed.confirm_import:
            return 0
    if parsed.confirm_import:
        return _confirm(parsed.confirm_import, runtime, locks_dir, out_stream)
    if parsed.preview:
        return _preview_path(parsed.preview, out_stream)
    return _interactive(in_stream, out_stream, picker)


def _confirm(path, runtime, locks_dir, out_stream):
    binding, _entry = load_binding(runtime)
    require_complete(binding)
    locks = MachineLock(locks_dir)
    lease = locks.acquire(binding.ledger, binding.account)
    try:
        assert_business_allowed(binding, lease, locks)
        receipt = confirm_import(path, runtime, binding, lease, locks)
    finally:
        locks.release(lease)
    _write(out_stream, f'导入已确认并回读。源文件名 {receipt["source_name"]}，未写钉钉。')
    return 0


def _parse_args(args):
    parser = argparse.ArgumentParser(add_help=True, description='工器具借还部署向导')
    parser.add_argument('--check-env', action='store_true', help='只打印环境检查后退出')
    parser.add_argument('--preview', metavar='FILE', help='预览指定文件，不弹出选择框')
    parser.add_argument('--runtime', metavar='DIR', help='本地运行目录（绑定与回执，不进 Git）')
    parser.add_argument('--lock-root', metavar='DIR', help='机器排他锁目录，默认与运行目录无关')
    parser.add_argument('--bind', metavar='FILE', help='从合成 JSON 写入绑定，已存在则拒绝覆盖')
    parser.add_argument('--confirm-import', metavar='FILE', help='预览后独立回读并保存导入回执，不写钉钉')
    parser.add_argument(
        '--require-ready',
        action='store_true',
        help='绑定或环境未通过时拒绝预览和交互。ready 文件不能放行。',
    )
    return parser.parse_args(args)


def _interactive(in_stream, out_stream, picker):
    choose = picker if picker is not None else pick_workbook_path
    while True:
        _write(out_stream, MENU.strip())
        _write(out_stream, '输入序号后按 Enter:')
        line = in_stream.readline()
        if line == '':
            _write(out_stream, '输入结束，退出。未启动后台进程。')
            return 0
        choice = line.strip()
        if choice in {'2', 'q', 'quit', 'exit'}:
            _write(out_stream, '已退出。关闭窗口不会留下业务进程。')
            return 0
        if choice != '1':
            _write(out_stream, '无法识别的选项。请输入 1 或 2。')
            continue
        selected = choose()
        if not selected:
            _write(out_stream, '已取消文件选择。源文件未修改，也未写入任何配置。')
            continue
        _preview_path(selected, out_stream)
    return 0


def _preview_path(path, out_stream):
    try:
        preview = preview_workbook(path)
    except PreviewError as exc:
        _write(out_stream, f'预览失败：{exc.message}')
        return 1
    _write(out_stream, format_preview(preview))
    return 0


def pick_workbook_path():
    try:
        from tkinter import Tk, filedialog
    except Exception as exc:
        raise RuntimeError(f'无法打开文件选择框：{exc}') from exc
    root = Tk()
    try:
        root.withdraw()
        try:
            root.wm_attributes('-topmost', 1)
        except Exception:
            pass
        selected = filedialog.askopenfilename(
            title='选择台账文件（只预览，不导入）',
            filetypes=[
                ('表格文件', '*.xls *.xlsx *.xlsm *.html *.htm'),
                ('所有文件', '*.*'),
            ],
        )
    finally:
        root.destroy()
    if not selected:
        return None
    return str(Path(selected))


def _write(stream, text):
    stream.write(text + '\n')
    stream.flush()
