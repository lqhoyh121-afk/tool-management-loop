"""机器锁的 pid + 心跳 + TTL 接管（issue #90）。

这一层只问锁本身：遗留槽会不会自愈、活着的人还抢不抢得进、报告分不分得清「被占用」与
「接管」。真机那一轮的输出由 ``tests/bootstrap/test_drive.py`` 里的两个 CLI 用例回答。

时间全部走注入的 :class:`Clock`：TTL 与心跳不靠真实等待（不 sleep），阈值也按测试自己的
秒数钉住，不依赖真实墙钟。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bootstrap.instance import (HOLDER_NAME, SCOPE_NAME, TAKEOVER_EXITED,
                                TAKEOVER_EXPIRED, TAKEOVER_NOTES, TTL_SECONDS,
                                MachineLock, format_refusal_lines,
                                format_takeover_lines, inspect_slot, pid_alive,
                                slot_path)
from contracts.model import Code, ContractError, Identity
from contracts.ports import LedgerScope, lease_key

SCOPE = LedgerScope('synthetic-org', 'synthetic-stock')
ACCOUNT = Identity('contact', 'synthetic-org', 'synthetic-manager')
OTHER_ACCOUNT = Identity('contact', 'synthetic-org', 'synthetic-other-manager')
#: 接管时整槽删掉：老版本/人工留下的额外文件不许活到下一轮。
LEFTOVER = 'left-over.txt'


def stamp(epoch):
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec='seconds')


def dead_pid():
    """一个**确证**不在运行的 pid：判活是接管的前提，找不到就当场炸，不猜。"""
    for candidate in range(999_999, 999_000, -1):
        if pid_alive(candidate) is False:
            return candidate
    raise AssertionError('这台机器上找不到一个确证不在运行的 pid')


class Clock:
    """可控时钟：TTL 与心跳刷新都按它算，测试不 sleep。

    起点取真实时间：槽目录 mtime 那一档（老版本留下的槽）用的是真实时钟，两者要能比。
    """

    def __init__(self, start=None):
        self.now = time.time() if start is None else start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class LockLeaseTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.lock_root = self.root / 'locks'
        self.clock = Clock()

    def tearDown(self):
        self._temp.cleanup()

    def lock(self, ttl=60, root=None):
        return MachineLock(root or self.lock_root, ttl_seconds=ttl, now=self.clock)

    def blocked(self, fn):
        with self.assertRaises(ContractError) as raised:
            fn()
        self.assertEqual(raised.exception.code, Code.INSTANCE)
        return raised.exception

    def abandon(self, scope=SCOPE, pid=999_999, heartbeat=None, started=None,
                holder=True, leftover=True, scope_file=True):
        """人为造一个遗留锁槽：持有进程被杀之后原地留下的就是这个样子。"""
        slot = slot_path(self.lock_root, scope)
        slot.mkdir(parents=True)
        if scope_file:
            (slot / SCOPE_NAME).write_text(lease_key(scope) + '\n', encoding='utf-8')
        if leftover:
            (slot / LEFTOVER).write_text('last round left this\n', encoding='utf-8')
        if holder:
            payload = {'pid': pid, 'started_at': started or '',
                       'heartbeat_at': heartbeat or ''}
            (slot / HOLDER_NAME).write_text(json.dumps(payload), encoding='utf-8')
        return slot

    def write_holder(self, slot, pid, heartbeat=None, started=None, token=''):
        (slot / HOLDER_NAME).write_text(json.dumps({
            'pid': pid, 'started_at': started or stamp(self.clock.now),
            'heartbeat_at': heartbeat or stamp(self.clock.now),
            'token': token}), encoding='utf-8')

    # --- 死锁自愈 -------------------------------------------------------------

    def test_an_abandoned_slot_is_taken_over_and_the_round_continues(self):
        """被杀进程留下的槽（pid 确证已死）→ 下一轮自动接管、整槽清理、继续跑。"""
        slot = self.abandon(pid=dead_pid(), heartbeat=stamp(self.clock.now - 5),
                            started=stamp(self.clock.now - 5))
        locks = self.lock()
        lease = locks.acquire(SCOPE, ACCOUNT)          # 不需要人工清槽
        self.assertEqual(len(locks.takeovers), 1)
        self.assertEqual(locks.takeovers[0].reason, TAKEOVER_EXITED)
        locks.assert_held(lease)
        self.assertTrue(slot.is_dir())
        # 整槽清理：老持有者的残留文件不许活到接手之后。
        self.assertFalse((slot / LEFTOVER).exists())
        holder = json.loads((slot / HOLDER_NAME).read_text(encoding='utf-8'))
        self.assertEqual(holder['pid'], os.getpid())
        self.assertEqual((slot / SCOPE_NAME).read_text(encoding='utf-8').strip(),
                         lease_key(SCOPE))
        locks.release(lease)

    def test_a_slot_whose_heartbeat_is_older_than_the_ttl_is_taken_over(self):
        """心跳超 TTL 就是遗留槽 —— 即使那个 pid 看起来还活着（老槽/pid 复用）。"""
        self.abandon(pid=os.getpid(), heartbeat=stamp(self.clock.now - 3600),
                     started=stamp(self.clock.now - 3600))
        locks = self.lock(ttl=60)
        lease = locks.acquire(SCOPE, ACCOUNT)
        self.assertEqual(locks.takeovers[0].reason, TAKEOVER_EXPIRED)
        locks.release(lease)

    def test_a_slot_without_a_holder_record_is_read_from_the_directory_mtime(self):
        """老版本留下的槽没有 holder.json：心跳退回槽目录 mtime，一样等得到自愈。"""
        slot = self.abandon(holder=False)
        fresh = self.lock(ttl=TTL_SECONDS)
        self.blocked(lambda: fresh.acquire(SCOPE, ACCOUNT))   # 刚动过的槽：判不准 → 拒绝
        self.assertEqual(fresh.takeovers, ())
        self.assertEqual(fresh.refusal.holder.heartbeat_source, 'slot-mtime')
        old = time.time() - (TTL_SECONDS + 60)
        os.utime(slot, (old, old))
        healer = self.lock(ttl=TTL_SECONDS)
        lease = healer.acquire(SCOPE, ACCOUNT)
        self.assertEqual(healer.takeovers[0].reason, TAKEOVER_EXPIRED)
        healer.release(lease)

    def test_a_working_holder_refreshes_its_heartbeat_and_keeps_the_lock(self):
        """真在干活的持有者不会因为轮次长就被判成死锁：闸门检查顺手刷新心跳。"""
        holder = self.lock(ttl=60)                     # 刷新间隔 = TTL/4 = 15 秒
        lease = holder.acquire(SCOPE, ACCOUNT)
        slot = slot_path(self.lock_root, SCOPE)
        self.write_holder(slot, os.getpid(), heartbeat=stamp(self.clock.now - 50))
        self.clock.advance(30)                         # 不刷新就超过 TTL(60)
        holder.assert_held(lease)                      # 每次写入前的闸门检查
        other = self.lock(ttl=60)
        self.blocked(lambda: other.acquire(SCOPE, ACCOUNT))
        self.assertEqual(other.takeovers, ())
        holder.release(lease)

    # --- 排他性一字未改 -------------------------------------------------------

    def test_a_live_holder_with_a_fresh_heartbeat_is_refused_not_taken_over(self):
        holder = self.lock()
        lease = holder.acquire(SCOPE, ACCOUNT)
        other = self.lock()
        self.blocked(lambda: other.acquire(SCOPE, ACCOUNT))
        self.assertEqual(other.takeovers, ())
        self.assertIsNotNone(other.refusal)
        self.assertTrue(other.refusal.alive_known)
        self.assertTrue(other.refusal.pid_alive)
        self.assertFalse(other.refusal.expired)
        self.assertFalse(other.refusal.takeable)
        holder.assert_held(lease)                      # 持有者一点没受影响
        holder.release(lease)
        recovered = other.acquire(SCOPE, ACCOUNT)      # 释放之后照样能拿
        other.release(recovered)

    def test_two_runtime_directories_share_one_machine_lock(self):
        """不同运行目录共用一把机器锁：活着的持有者仍然互斥，遗留槽仍然自愈。"""
        first = self.lock()
        lease = first.acquire(SCOPE, ACCOUNT)
        second = self.lock()                            # 另一个运行目录的第二个实例
        self.blocked(lambda: second.acquire(SCOPE, OTHER_ACCOUNT))
        slot = slot_path(self.lock_root, SCOPE)
        self.write_holder(slot, dead_pid(), heartbeat=stamp(self.clock.now - 5))
        taken = second.acquire(SCOPE, OTHER_ACCOUNT)
        self.assertEqual(len(second.takeovers), 1)
        second.assert_held(taken)
        # 原持有者收尾时不许删掉接手者的槽（那会同时放宽排他）。
        first.release(lease)
        self.assertTrue(slot.is_dir())
        self.assertEqual(first.released_stolen, (slot,))
        second.release(taken)

    def test_the_slot_path_is_the_documented_legacy_path(self):
        """槽路径（sha256(lease_key)）不因这次改造改名：升级后旧槽照样被认出来。"""
        digest = sha256(lease_key(SCOPE).encode('utf-8')).hexdigest()
        self.assertEqual(slot_path(self.lock_root, SCOPE), self.lock_root / digest)

    # --- 不猜：判不出来就拒绝 ------------------------------------------------

    def test_a_slot_whose_holder_record_is_unreadable_is_refused(self):
        slot = self.abandon(holder=False)
        (slot / HOLDER_NAME).write_text('{not json at all', encoding='utf-8')
        locks = self.lock(ttl=TTL_SECONDS)
        self.blocked(lambda: locks.acquire(SCOPE, ACCOUNT))
        self.assertIsNotNone(locks.refusal)
        self.assertIsNone(locks.refusal.holder.pid)
        self.assertFalse(locks.refusal.takeable)

    def test_a_holder_record_without_a_positive_pid_is_refused(self):
        for pid in ('not-a-pid', 0, -1, True, None):
            with self.subTest(pid=pid):
                root = self.root / f'locks-{str(pid).replace("-", "minus")}'
                slot = slot_path(root, SCOPE)
                slot.mkdir(parents=True)
                (slot / HOLDER_NAME).write_text(json.dumps({'pid': pid}),
                                                encoding='utf-8')
                locks = MachineLock(root, ttl_seconds=60, now=self.clock)
                self.blocked(lambda: locks.acquire(SCOPE, ACCOUNT))
                self.assertIsNone(locks.refusal.holder.pid)
                self.assertFalse(locks.refusal.takeable)

    def test_release_never_deletes_a_slot_that_was_taken_over(self):
        """心跳过期后被别的实例接了班：原持有者的收尾**不许**删掉新持有者的锁。"""
        holder = self.lock()
        lease = holder.acquire(SCOPE, ACCOUNT)
        slot = slot_path(self.lock_root, SCOPE)
        self.write_holder(slot, dead_pid(), token='another-instances-token')
        holder.release(lease)
        self.assertTrue(slot.is_dir())
        self.assertEqual(holder.released_stolen, (slot,))
        self.assertEqual(
            json.loads((slot / HOLDER_NAME).read_text(encoding='utf-8'))['token'],
            'another-instances-token')

    def test_the_liveness_probe_is_non_destructive(self):
        """判活只看状态、不发信号（Windows 下 os.kill 会把那个 pid 真的杀掉）。"""
        me = os.getpid()
        self.assertIs(pid_alive(me), True)
        self.assertIs(pid_alive(me), True)
        self.assertIs(pid_alive(dead_pid()), False)
        self.assertIsNone(pid_alive(0))
        self.assertIsNone(pid_alive('not-a-pid'))

    # --- 报告口径 -------------------------------------------------------------

    def test_the_report_tells_occupied_apart_from_takeover(self):
        holder = self.lock()
        lease = holder.acquire(SCOPE, ACCOUNT)
        blocked = self.lock()
        self.blocked(lambda: blocked.acquire(SCOPE, ACCOUNT))
        refusal = '\n'.join(format_refusal_lines(blocked))
        self.assertIn('上一轮未结束，本轮跳过', refusal)
        self.assertIn('SECOND_INSTANCE_BLOCKED', refusal)
        self.assertIn('未接管', refusal)
        self.assertIn('退出码 0', refusal)
        self.assertNotIn('锁接管', refusal)

        slot = slot_path(self.lock_root, SCOPE)
        holder.release(lease)
        self.abandon(pid=dead_pid(), heartbeat=stamp(self.clock.now - 5),
                     started=stamp(self.clock.now - 5))
        taker = self.lock()
        taker.acquire(SCOPE, ACCOUNT)
        takeover = '\n'.join(format_takeover_lines(taker))
        self.assertIn('锁接管', takeover)
        self.assertIn('不需要人工清槽', takeover)
        self.assertIn(TAKEOVER_NOTES[TAKEOVER_EXITED], takeover)
        self.assertNotIn('本轮跳过', takeover)

    def test_inspect_answers_who_holds_and_why(self):
        self.assertIsNone(inspect_slot(self.lock_root, SCOPE, 60, self.clock.now))
        self.abandon(pid=dead_pid(), heartbeat=stamp(self.clock.now - 5),
                     started=stamp(self.clock.now - 30))
        state = inspect_slot(self.lock_root, SCOPE, 60, self.clock.now)
        self.assertTrue(state.takeable)
        self.assertEqual(state.reason, TAKEOVER_EXITED)
        self.assertAlmostEqual(state.running_seconds, 30, delta=1)
        self.assertEqual(state.scope_key, lease_key(SCOPE))


if __name__ == '__main__':
    unittest.main()
