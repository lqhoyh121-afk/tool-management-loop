"""Machine-wide SingleInstance using atomic directory create. No TTL takeover."""
from hashlib import sha256
from pathlib import Path

from contracts.model import Code, ContractError, require
from contracts.ports import lease_key


class MachineLock:
    """Exclusive writer keyed only by lease_key(scope), not runtime dir or account."""

    def __init__(self, lock_root):
        self.lock_root = Path(lock_root)
        self.held = {}

    def acquire(self, scope, account):
        require(account.namespace == 'contact', Code.IDENTITY)
        key = lease_key(scope)
        digest = sha256(key.encode('utf-8')).hexdigest()
        slot = self.lock_root / digest
        self.lock_root.mkdir(parents=True, exist_ok=True)
        try:
            slot.mkdir()
        except FileExistsError as exc:
            raise ContractError(Code.INSTANCE) from exc
        (slot / 'scope').write_text(key + '\n', encoding='utf-8')
        lease = 'lease-' + digest[:16]
        self.held[lease] = slot
        return lease

    def assert_held(self, lease):
        require(isinstance(lease, str) and lease in self.held, Code.INSTANCE)
        slot = self.held[lease]
        require(slot.is_dir(), Code.INSTANCE)

    def release(self, lease):
        self.assert_held(lease)
        slot = self.held.pop(lease)
        for child in slot.iterdir():
            child.unlink()
        slot.rmdir()
