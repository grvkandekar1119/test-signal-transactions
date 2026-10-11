"""
test_signal_transactions.py  -  Do Django signals run in the same DB transaction as the caller?

Answer: YES, by default. Signals are plain synchronous function calls made
inside Model.save(), so they run on the caller's thread and therefore on the
caller's DB connection. Signals never open their own transaction and never
use a separate connection: they simply JOIN whatever transaction the caller
is in. If the caller is not in a transaction (Django's default autocommit
mode), the receiver is not in one either.

How to use:
  1. Copy this file into any app of a Django project (e.g. myapp/test_signal_transactions.py).
  2. Run:  python manage.py test

We use TransactionTestCase (not TestCase) because TestCase wraps every test
in its own atomic block, which would hide what we want to observe.

Proof 1 (rollback)   : receiver writes a Group row; the caller's atomic block
                       fails -> BOTH the User and the receiver's Group vanish.
                       Only possible if they shared one transaction.
Proof 2 (same conn)  : receiver sees the same connection object and
                       connection.in_atomic_block == True inside the caller's atomic.
Proof 3 (on_commit)  : transaction.on_commit() registered inside the receiver
                       does not fire until the CALLER's atomic block exits.
Proof 4 (autocommit) : with no caller transaction, the receiver is not in one
                       either (signals don't create a transaction of their own).
"""
from django.contrib.auth.models import Group, User
from django.db import connection, transaction
from django.db.models.signals import post_save
from django.test import TransactionTestCase


class SignalTransactionTests(TransactionTestCase):
    def setUp(self):
        self.seen = {}

        def receiver_fn(sender, instance, created, **kwargs):
            if not created:
                return
            self.seen["in_atomic_block"] = connection.in_atomic_block
            self.seen["connection_id"] = id(connection.connection)
            # The receiver does its own DB write:
            Group.objects.create(name=f"group-for-{instance.username}")
            # And registers a commit hook:
            transaction.on_commit(lambda: self.seen.setdefault("on_commit_fired", True))

        self.receiver_fn = receiver_fn
        post_save.connect(receiver_fn, sender=User, weak=False)

    def tearDown(self):
        post_save.disconnect(self.receiver_fn, sender=User)

    # ---- Proof 1: rollback of the caller also rolls back the receiver's write
    def test_receiver_write_is_rolled_back_with_caller(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                User.objects.create_user(username="alice", password="x")
                # The receiver already ran and wrote a Group inside this block:
                self.assertEqual(Group.objects.filter(name="group-for-alice").count(), 1)
                raise RuntimeError("caller fails -> whole transaction must roll back")

        self.assertEqual(User.objects.filter(username="alice").count(), 0)
        # The Group was written by the RECEIVER, yet it is gone too:
        self.assertEqual(Group.objects.filter(name="group-for-alice").count(), 0)

    # ---- Proof 2: receiver runs on the same connection, inside the caller's atomic
    def test_receiver_shares_connection_and_atomic_block(self):
        with transaction.atomic():
            caller_conn_id = id(connection.connection)
            User.objects.create_user(username="bob", password="x")

        self.assertTrue(self.seen["in_atomic_block"])
        self.assertEqual(self.seen["connection_id"], caller_conn_id)

    # ---- Proof 3: on_commit hooks from the receiver wait for the CALLER's commit
    def test_on_commit_in_receiver_waits_for_caller_commit(self):
        with transaction.atomic():
            User.objects.create_user(username="carol", password="x")
            # Receiver has run, but the transaction is still open:
            self.assertNotIn("on_commit_fired", self.seen)
        # Outer atomic exited (committed) -> hook fires now:
        self.assertTrue(self.seen["on_commit_fired"])

    # ---- Proof 4: no caller transaction -> receiver is not in a transaction either
    def test_no_caller_transaction_means_no_transaction_in_receiver(self):
        self.assertFalse(connection.in_atomic_block)
        User.objects.create_user(username="dave", password="x")
        self.assertFalse(self.seen["in_atomic_block"])
