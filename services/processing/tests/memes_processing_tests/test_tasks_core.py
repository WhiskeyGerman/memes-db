from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from memes_kernel.values import Number
from memes_processing.application.tasks import (
    AttemptLease,
    ProcessingTask,
    RetryPolicy,
    SchedulingError,
    SchedulingViolation,
    TaskFailure,
    TaskLimit,
    TaskState,
)


@pytest.fixture
def lease(now: datetime) -> AttemptLease:
    return AttemptLease(
        id=uuid4(),
        worker_id=uuid4(),
        started_at=now,
        expires_at=now + timedelta(seconds=TaskLimit.LEASE_SECONDS),
        deadline=now + timedelta(seconds=TaskLimit.TASK_SECONDS),
    )


def test_no_workers_does_not_consume_attempt(task: ProcessingTask) -> None:
    assert task.state is TaskState.WAITING
    assert task.attempts == Number.ZERO


def test_stale_and_expired_attempts_cannot_complete(
    task: ProcessingTask, lease: AttemptLease, now: datetime
) -> None:
    assigned = task.assign(lease=lease, now=now)
    with pytest.raises(SchedulingError) as stale:
        assigned.accept(attempt_id=uuid4(), now=now)
    assert stale.value.code is SchedulingViolation.STALE_ATTEMPT
    with pytest.raises(SchedulingError) as expired:
        assigned.accept(attempt_id=lease.id, now=lease.expires_at)
    assert expired.value.code is SchedulingViolation.EXPIRED_LEASE


def test_lease_expiration_consumes_attempt_and_schedules_retry(
    task: ProcessingTask, lease: AttemptLease, now: datetime
) -> None:
    assigned = task.assign(lease=lease, now=now)
    retry = assigned.fail(
        attempt_id=lease.id,
        reason=TaskFailure.LEASE_EXPIRED,
        policy=RetryPolicy(),
        random_fraction=float(Number.ZERO),
        now=lease.expires_at,
    )
    assert retry.state is TaskState.RETRY_WAIT
    assert retry.attempts == Number.ONE
    assert retry.ready_at == lease.expires_at + RetryPolicy().base_delay


def test_invalid_input_is_not_retried(
    task: ProcessingTask, lease: AttemptLease, now: datetime
) -> None:
    failed = task.assign(lease=lease, now=now).fail(
        attempt_id=lease.id,
        reason=TaskFailure.INVALID_INPUT,
        policy=RetryPolicy(),
        random_fraction=float(Number.ZERO),
        now=now,
    )
    assert failed.state is TaskState.FAILED


def test_three_retries_follow_first_attempt(
    task: ProcessingTask, lease: AttemptLease, now: datetime
) -> None:
    policy = RetryPolicy()
    for attempt in range(policy.maximum_retries + Number.ONE):
        lease = replace(
            lease,
            id=uuid4(),
            started_at=now,
            expires_at=now + timedelta(seconds=TaskLimit.LEASE_SECONDS),
            deadline=now + timedelta(seconds=TaskLimit.TASK_SECONDS),
        )
        task = task.assign(lease=lease, now=now).fail(
            attempt_id=lease.id,
            reason=TaskFailure.TEMPORARY,
            policy=policy,
            random_fraction=float(Number.ZERO),
            now=now,
        )
        if attempt < policy.maximum_retries:
            assert task.state is TaskState.RETRY_WAIT
            assert task.ready_at is not None
            now = task.ready_at
    assert task.state is TaskState.FAILED
    assert task.attempts == policy.maximum_retries + Number.ONE


def test_exponential_backoff_cap_and_jitter() -> None:
    policy = RetryPolicy(maximum_retries=10)
    assert policy.delay(Number.ONE, random_fraction=float(Number.ZERO)) == policy.base_delay
    assert policy.delay(Number.ONE, random_fraction=float(Number.ONE)) == policy.base_delay * (
        Number.ONE + policy.jitter_fraction
    )
    assert (
        policy.delay(policy.maximum_retries, random_fraction=float(Number.ZERO))
        == policy.maximum_delay
    )


def test_renewal_cannot_exceed_deadline(lease: AttemptLease, now: datetime) -> None:
    renewed = lease.renew(
        attempt_id=lease.id, now=now, duration=timedelta(seconds=TaskLimit.TASK_SECONDS)
    )
    assert renewed.expires_at == lease.deadline


def test_cancellation_rejects_late_result(
    task: ProcessingTask, lease: AttemptLease, now: datetime
) -> None:
    cancelled = task.assign(lease=lease, now=now).cancel()
    with pytest.raises(SchedulingError):
        cancelled.accept(attempt_id=lease.id, now=now)
