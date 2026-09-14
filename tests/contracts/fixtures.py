"""Synthetic fixtures shared without depending on test-discovery module names."""
from dataclasses import replace
from datetime import datetime, timezone

from contracts.model import Identity, Resource, Loan, Inventory, Event, IdentityBinding
from contracts.flow import plan, verify, Action, Receipt, Outcome

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
BORROWER = Identity("contact", "synthetic-org", "synthetic-borrower")
MANAGER = Identity("contact", "synthetic-org", "synthetic-manager")
LOAN = Resource("record", "synthetic-org", "synthetic-loans", "synthetic-loan")
ITEM = Resource("record", "synthetic-org", "synthetic-stock", "synthetic-item")
FORM = Resource("form", "synthetic-org", "synthetic-forms", "synthetic-form")


def loan():
    return Loan(LOAN, ITEM, BORROWER, MANAGER, MANAGER, 2, False, (),
                NOW, "synthetic-config-v1", application_evidence="synthetic-application-read")


def event(action, actor=MANAGER):
    return Event(action, "synthetic-" + action.value, LOAN, FORM, actor,
                 NOW, "synthetic-config-v1", "form", True,
                 evidence_ref="synthetic-source-readback")


def stock():
    return Inventory(ITEM, 5, 0, 0, (), (), (), "synthetic-rev-1")


def completion(action):
    task = Resource("todo", "synthetic-org", "synthetic-todos", "synthetic-" + action.value)
    internal = Identity("todo", "synthetic-org", "synthetic-internal-manager")
    binding = IdentityBinding(MANAGER, internal, task, "synthetic-create", "synthetic-get")
    return replace(event(action), actor=internal, source=task,
                   evidence_kind="todo_completion", binding=binding)


def settled(current, action_event, inventory):
    intent = plan(current, action_event, inventory)
    receipt = Receipt(intent.operation_id, Outcome.VERIFIED, "synthetic-readback",
                      intent.after, intent.stock_after)
    return verify(intent, receipt).loan, intent.stock_after
