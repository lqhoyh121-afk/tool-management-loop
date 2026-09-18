"""Machine-wide SingleInstance: atomic slot, pid + heartbeat, TTL takeover.

排他键仍是 ``lease_key(scope)``（租户 + 账本容器；不含运行目录、账号、物品），槽路径
``lock_root/<sha256(lease_key(scope))>`` 与旧实现逐字相同 —— 升级后遗留的旧槽照样被认出来。

槽里两样东西：``scope``（原样保留，人工排查用）与 ``holder.json``
（``{"pid": …, "started_at": …, "heartbeat_at": …}``）；心跳读不出来时退回**槽目录自身的
修改时间**，所以老版本留下的槽也有心跳可判，升级那一次不需要人工清槽。

三条规则（``docs/deployment.md`` 是同一份口径）：

* **心跳**：持有者在 :meth:`MachineLock.assert_held`（每次业务写入前的闸门检查）里前移
  ``heartbeat_at``。真在干活的实例永远看起来是新鲜的；只有被杀、断电或卡死的持有者才会
  「心跳变旧」。
* **TTL 接管**：心跳比 :data:`TTL_SECONDS` 还旧 → 按遗留锁槽处置：删掉整个槽、重试一次
  占用。取值理由见 :data:`TTL_SECONDS`。
* **判活接管**：槽里记的 pid 能**确证不在运行**时不必等满 TTL（进程被杀后立刻自愈）。判活
  **非破坏**：Windows 走 ``ctypes`` 的 ``OpenProcess`` / ``GetExitCodeProcess``；其余平台走
  ``os.kill(pid, 0)``（signal 0 不投递任何信号，只做存在性探测）。判不出来（没权限打开、
  pid 读不出）按**活着**处理：只拒绝、不接管。**任何时候都不给别的进程发信号。**

排他性一字未改：接管只请走**已确证死亡或心跳过期**的持有者；两个运行目录共用同一把机器锁
时，活着的持有者仍然互斥（第二实例 ``SECOND_INSTANCE_BLOCKED``）。接管与「被占用」是两件
事：:attr:`MachineLock.takeovers` 与 :attr:`MachineLock.refusal` 分开记，
:func:`format_takeover_lines` / :func:`format_refusal_lines` 分开说。
"""
import ctypes
import json
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from contracts.model import Code, ContractError, require
from contracts.ports import lease_key

#: 遗留锁槽的判定上限（秒）。单轮驱动实测 60~100 秒；定时脚本给驱动子进程的超时是 280 秒
#: （那个脚本不在本仓库）。取 900 秒（15 分钟）≈ 9 倍实测单轮 / 3 倍杀超时：真在跑的轮次
#: 不可能被判成遗留，真死掉的槽最迟 TTL 之后第一个定时轮就自愈，不需要人工清槽。
TTL_SECONDS = 900
#: 槽内文件名。``scope`` 是原样保留的排查线索，``holder.json`` 是判活与 TTL 的判据。
SCOPE_NAME = 'scope'
HOLDER_NAME = 'holder.json'

#: 接管原因码：只进报告，不进公共 Code 空间（业务码是 ``contracts.model.Code``）。
TAKEOVER_EXITED = 'holder_exited'
TAKEOVER_EXPIRED = 'heartbeat_expired'
TAKEOVER_NOTES = {
    TAKEOVER_EXITED: '持有进程已退出，判活确证不在运行',
    TAKEOVER_EXPIRED: '心跳超时（≥ TTL，按遗留锁槽处置）',
}
#: 槽里没登记 pid 时报告要说的话：不允许把「说不清」读成「没人占」。
HOLDER_PID_MISSING = '未登记 pid（老版本或人工造的槽，按活着处理）'


def _now_epoch():
    return time.time()


def _stamp(epoch):
    """epoch 秒 → 本地时区的 ISO 时间戳（带偏移，报告里能直接读）。"""
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec='seconds')


def _epoch_of(value):
    """ISO 时间戳 → epoch 秒；不是时间戳就返回 None（不猜）。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip()).timestamp()
    except ValueError:
        return None


def slot_path(lock_root, scope):
    """这台机器上这个账本作用域的锁槽路径（与运行目录、账号、物品无关）。"""
    return Path(lock_root) / sha256(lease_key(scope).encode('utf-8')).hexdigest()


def pid_alive(pid):
    """判活（非破坏）：``True`` 在运行 / ``False`` 已确证不在运行 / ``None`` 判不出来。

    **绝不用信号探活**：Windows 下 ``os.kill(pid, 0)`` 会把那个 pid 真的杀掉，所以这里走
    Win32 API；其余平台 ``os.kill(pid, 0)`` 只做存在性探测、不投递任何信号。返回 ``None``
    一律按「活着」处理 —— 判不出来不许放宽排他。
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == 'nt':
        return _windows_pid_alive(pid)
    return _posix_pid_alive(pid)


def _windows_pid_alive(pid):
    """``OpenProcess`` + ``GetExitCodeProcess``：只读进程状态，绝不 ``TerminateProcess``。"""
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    ERROR_ACCESS_DENIED = 5
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
    if not handle:
        # 打不开：没权限说明进程在、只是读不到状态 → 判不出来（按活着处理）；
        # 其余错误（含 pid 不存在）是「确证不在运行」。
        return None if ctypes.get_last_error() == ERROR_ACCESS_DENIED else False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _posix_pid_alive(pid):
    """``os.kill(pid, 0)``：signal 0 不投递信号，只做存在性探测。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError:
        return None
    return True


@dataclass(frozen=True)
class Holder:
    """槽里记的持有者事实；读不出来的字段留空/None，绝不补默认值。"""

    pid: object
    started_at: str
    heartbeat_at: str
    heartbeat_age: float
    heartbeat_source: str


@dataclass(frozen=True)
class SlotState:
    """一个锁槽在某一刻的现状。接管与否**只**由它回答。"""

    slot: Path
    scope_key: str
    holder: Holder
    pid_alive: bool
    alive_known: bool
    expired: bool
    now: float

    @property
    def takeable(self):
        """能不能接管：**心跳过期**，或**判活确证不在运行**。别的一律不许接管。"""
        return self.expired or (self.alive_known and not self.pid_alive)

    @property
    def reason(self):
        if self.expired:
            return TAKEOVER_EXPIRED
        if self.alive_known and not self.pid_alive:
            return TAKEOVER_EXITED
        return ''

    @property
    def running_seconds(self):
        """持有者已经占了多久（记了起始时间才有；读不出来返回 None）。"""
        epoch = _epoch_of(self.holder.started_at)
        if epoch is None:
            return None
        return max(0.0, self.now - epoch)


def inspect_slot(lock_root, scope, ttl_seconds=TTL_SECONDS, now=None, alive=None):
    """读一个锁槽：谁在占、心跳多久没动、能不能接管；槽不在时返回 ``None``。"""
    if now is None:
        now = _now_epoch()
    if alive is None:
        alive = pid_alive
    slot = slot_path(lock_root, scope)
    if not slot.is_dir():
        return None
    holder = _read_holder(slot, now)
    verdict = None if holder.pid is None else alive(holder.pid)
    return SlotState(slot=slot, scope_key=lease_key(scope), holder=holder,
                     pid_alive=verdict is True, alive_known=verdict is not None,
                     expired=holder.heartbeat_age >= ttl_seconds, now=now)


def _read_holder(slot, now):
    """``holder.json`` + 槽目录 mtime → 持有者与心跳年龄。

    读不出来（文件缺、JSON 坏、pid 不是正整数）不是「没有持有者」，而是「这个槽说不清是
    谁在占」：pid 留空、心跳退回槽目录的修改时间 —— 老版本留下的槽与人工造的槽走这一路，
    一样有 TTL 可判。
    """
    try:
        data = json.loads((slot / HOLDER_NAME).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        data = {}
    raw_pid = data.get('pid')
    pid = raw_pid if (isinstance(raw_pid, int) and not isinstance(raw_pid, bool)
                      and raw_pid > 0) else None
    started_at = data.get('started_at') if isinstance(data.get('started_at'), str) else ''
    heartbeat_at = data.get('heartbeat_at') if isinstance(data.get('heartbeat_at'), str) else ''
    epoch = _epoch_of(heartbeat_at)
    if epoch is None:
        # 心跳读不出来：退回槽目录自身的修改时间（老槽、手写的槽都还有得判）。
        heartbeat_at = ''
        epoch = _slot_mtime(slot, now)
    return Holder(pid=pid, started_at=started_at, heartbeat_at=heartbeat_at,
                  heartbeat_age=max(0.0, now - epoch),
                  heartbeat_source='holder.json' if heartbeat_at else 'slot-mtime')


def _slot_mtime(slot, now):
    """槽目录的修改时间；读不到按「刚动过」处理（不许凭读不到就接管）。"""
    try:
        return slot.stat().st_mtime
    except OSError:
        return now


def _holder_text(state):
    """持有者一句话：pid、判活结论、起始时间、已占多久、心跳多久没动。"""
    holder = state.holder
    if holder.pid is None:
        parts = [HOLDER_PID_MISSING]
    else:
        verdict = {True: '判活：仍在运行', False: '判活：已确证不在运行',
                   None: '判活：判不出来（按活着处理）'}[
                       state.pid_alive if state.alive_known else None]
        parts = ['pid={pid}（{verdict}）'.format(pid=holder.pid, verdict=verdict)]
    if holder.started_at:
        parts.append('起始={start}'.format(start=holder.started_at))
    running = state.running_seconds
    if running is not None:
        parts.append('已占用 {seconds} 秒'.format(seconds=int(running)))
    parts.append('心跳 {seconds} 秒前（来源 {source}）'.format(
        seconds=int(holder.heartbeat_age), source=holder.heartbeat_source))
    return '，'.join(parts)


def format_takeover_lines(locks):
    """本进程**接管**过的锁槽（死锁自愈）：接管了谁、为什么、槽在哪。

    与「被占用」分开说：接管是「上一轮的持有者已经死掉或心跳过期，这一轮接上去继续跑」，
    被占用是「另一个实例正在跑，这一轮什么也不做」。
    """
    lines = []
    for state in locks.takeovers:
        lines.append('锁接管：遗留锁槽已清理并重新占用（{note}），本轮继续，不需要人工清槽。'
                     .format(note=TAKEOVER_NOTES.get(state.reason, state.reason)))
        lines.append('  被接管者：{detail}'.format(detail=_holder_text(state)))
        lines.append('  槽={slot}'.format(slot=state.slot))
    return tuple(lines)


def format_refusal_lines(locks):
    """被拒时的说明：谁在占、占了多久、是不是死锁；「本轮跳过」说在明处。"""
    lines = ['本轮跳过：上一轮未结束，本轮跳过（机器锁被占用中，本轮不做任何业务写入）。']
    state = locks.refusal
    if state is None:
        lines.append('  SECOND_INSTANCE_BLOCKED 占用者：槽状态读不出来（槽已被释放或形态'
                     '没见过），按排他拒绝处理。')
    else:
        lines.append('  SECOND_INSTANCE_BLOCKED 持有者：{detail}'.format(
            detail=_holder_text(state)))
        lines.append('  槽={slot}'.format(slot=state.slot))
    lines.append('  未接管：心跳在 TTL {ttl} 秒内、且判活不成立「已死」，按「另一实例正常'
                 '在跑」处理。'.format(ttl=locks.ttl_seconds))
    lines.append('  本轮跳过不算失败（退出码 0）：占用中说明另一实例正在推进，等下一轮即可；'
                 '死锁残留由 TTL 自愈，不需要人工清槽。')
    return tuple(lines)


class MachineLock:
    """Exclusive writer keyed only by lease_key(scope), not runtime dir or account."""

    def __init__(self, lock_root, ttl_seconds=None, now=None, alive=None):
        self.lock_root = Path(lock_root)
        self.ttl_seconds = TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
        require(self.ttl_seconds > 0)
        #: 心跳刷新间隔：持有者每过 TTL/4 才重写一次 holder.json，少做无谓写盘。
        self.heartbeat_seconds = max(1, self.ttl_seconds // 4)
        self.held = {}
        #: 本进程这一轮接管过的槽（报告用）。
        self.takeovers = ()
        #: 最后一次被拒时槽的现状（None = 槽状态读不出来）。
        self.refusal = None
        #: 释放时发现槽已不属于本进程（被接管过）：只记账，绝不删别人的槽。
        self.released_stolen = ()
        self._now = now if now is not None else _now_epoch
        self._alive = alive if alive is not None else pid_alive
        self._started = {}
        self._heartbeat = {}
        self._tokens = {}

    def acquire(self, scope, account):
        require(account.namespace == 'contact', Code.IDENTITY)
        key = lease_key(scope)
        digest = sha256(key.encode('utf-8')).hexdigest()
        slot = self.lock_root / digest
        self.lock_root.mkdir(parents=True, exist_ok=True)
        pending = None
        # 最多三轮：占坑 → （撞上遗留槽则清理）→ 重试。抢输了就是普通拒绝。
        for _attempt in range(3):
            try:
                slot.mkdir()
            except FileExistsError:
                state = self.inspect(scope)
                if state is not None and state.takeable:
                    self._clear(state.slot)
                    pending = state
                    continue
                self.refusal = state
                raise ContractError(Code.INSTANCE) from None
            except OSError as exc:
                # 槽路径是个文件、或锁根不可写这类形态：不猜，直接排他拒绝。
                raise ContractError(Code.INSTANCE) from exc
            self._write(slot, key)
            if pending is not None:
                self.takeovers = self.takeovers + (pending,)
            lease = 'lease-' + digest[:16]
            self.held[lease] = slot
            return lease
        raise ContractError(Code.INSTANCE)

    def inspect(self, scope):
        """当前槽的现状（谁在占、能不能接管）；槽不在时返回 ``None``。"""
        return inspect_slot(self.lock_root, scope, self.ttl_seconds, self._now(), self._alive)

    def assert_held(self, lease):
        slot = self._held_slot(lease)
        now = self._now()
        # 闸门检查顺手把心跳前移：真在干活的实例永远新鲜，只有被杀/卡死的才会「心跳变旧」。
        if now - self._heartbeat.get(slot, 0.0) >= self.heartbeat_seconds:
            self._touch(slot, now)

    def release(self, lease):
        """只释放自己的槽。

        槽已经不属于本进程时**不删**：被接管过的持有者（心跳过期后别的实例接了班）走到这里
        时删掉它，等于把新持有者的排他锁拆掉、两个写入者同时上台。所以先按槽里记的 ``token``
        认人，不是自己的就只忘掉租约并记账（``released_stolen``）。
        """
        slot = self._held_slot(lease)
        self.held.pop(lease)
        self._started.pop(slot, None)
        self._heartbeat.pop(slot, None)
        token = self._tokens.pop(slot, None)
        if not self._is_owner(slot, token):
            self.released_stolen = self.released_stolen + (slot,)
            return
        for child in slot.iterdir():
            child.unlink()
        slot.rmdir()

    def _held_slot(self, lease):
        require(isinstance(lease, str) and lease in self.held, Code.INSTANCE)
        slot = self.held[lease]
        require(slot.is_dir(), Code.INSTANCE)
        return slot

    def _is_owner(self, slot, token):
        """槽里记的 ``token`` 是不是本进程这一轮的；读不出来/对不上按「不是」处理。

        用每次占用新生成的随机 ``token`` 而不是 pid：同一台机器上 pid 会被复用，而且
        「另一个实例接手了同一个槽」恰恰是必须认出来的那一幕。
        """
        if not isinstance(token, str) or not token:
            return False
        try:
            data = json.loads((slot / HOLDER_NAME).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return False
        return isinstance(data, dict) and data.get('token') == token

    def _clear(self, slot):
        """清理一个**已确证遗留**的槽：整槽删掉（槽里可能有残留文件，逐个删会留缝）。"""
        try:
            shutil.rmtree(slot)
        except OSError:
            # 删不动（权限、或别的实例同时删掉了）：不抛，交给下一轮 mkdir 定胜负。
            pass

    def _write(self, slot, key):
        (slot / SCOPE_NAME).write_text(key + '\n', encoding='utf-8')
        now = self._now()
        self._started[slot] = _stamp(now)
        # 这一轮槽的随机凭据：收尾时按它认自己的槽（pid 会被复用，认不出来就不许删）。
        self._tokens[slot] = uuid4().hex
        self._touch(slot, now)

    def _touch(self, slot, now):
        """把心跳前移到现在（重写 ``holder.json``，槽目录 mtime 一起更新）。

        写不动不抛：租约还在手上，心跳坏掉最坏是让别的实例在 TTL 之后接管 —— 那正是 TTL
        该管的事，不该在业务写入路径上抛异常。
        """
        payload = {'pid': os.getpid(),
                   'started_at': self._started.get(slot) or _stamp(now),
                   'heartbeat_at': _stamp(now),
                   'token': self._tokens.get(slot, '')}
        try:
            tmp = slot / (HOLDER_NAME + '.tmp')
            tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + '\n',
                           encoding='utf-8')
            tmp.replace(slot / HOLDER_NAME)
        except OSError:
            pass
        self._heartbeat[slot] = now
