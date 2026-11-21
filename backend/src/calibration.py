"""Interval-band calibration: pick a narrower band on the early half, blind-test on the later half.

Rules encoded here:
- Labelled cases are split at their observation time into an earlier selection half and a later blind half.
- The selection half may only choose a band at least as narrow as the current one, provided the
  missed-priority ratio does not rise.
- The blind half must show strictly fewer referrals and a missed-priority ratio no higher than the
  current band. Only then may the band be activated.
- A sample snapshot is frozen per evaluation. Retrying after a failure reuses that snapshot instead
  of recording a second evaluation. Labels that arrive after the snapshot feed the next evaluation.
- Activating one of two qualified submissions re-runs the other against the newly active band.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session as DatabaseSession

from .classifier import BAND as DEFAULT_BAND
from .models import BandCalibration, BandPolicy, CalibrationSample, CalibrationState, DelayIncident
from .planning import Rejected, audit, find, utc_now

STEP = 0.01
EPSILON = 1e-9


def current_band(db: DatabaseSession, *, lock=False):
    """Read the active band; register() seeds the row, so a miss here only means an empty database."""
    policy = db.get(BandPolicy, 1, with_for_update=lock)
    return policy.band if policy is not None else DEFAULT_BAND


def stats(scores, labels, band):
    priorities = sum(1 for label in labels if label)
    missed = sum(1 for score, label in zip(scores, labels) if label and score < -band)
    return {
        'referrals': sum(1 for score in scores if abs(score) <= band),
        'priorities': priorities,
        'missed': missed,
        'miss_ratio': missed / priorities if priorities else 0.0,
    }


def sample_flags(score, priority, baseline, candidate):
    """Per-sample audit trail: which side of each band the frozen score fell on."""
    return {
        'baseline_referral': abs(score) <= baseline,
        'baseline_missed': bool(priority) and score < -baseline,
        'candidate_referral': None if candidate is None else abs(score) <= candidate,
        'candidate_missed': None if candidate is None else bool(priority) and score < -candidate,
    }


def candidates(baseline):
    return [round(index * STEP, 2) for index in range(1, int(round(baseline / STEP)))]


def select_band(scores, labels, baseline):
    """Narrowest band whose missed-priority ratio on the selection half does not rise."""
    observed = stats(scores, labels, baseline)
    feasible = [candidate for candidate in candidates(baseline)
                if stats(scores, labels, candidate)['miss_ratio'] <= observed['miss_ratio'] + EPSILON]
    return (min(feasible) if feasible else None), observed


def _score(item):
    if isinstance(item, CalibrationSample):
        return None if item.score is None else float(item.score)
    if not item.result:
        return None
    try:
        return float(item.result['score'])
    except (KeyError, TypeError, ValueError):
        return None


def _split(records):
    """Split observations into an earlier selection half and a later blind half at a time boundary."""
    ordered = sorted(records, key=lambda item: (item.observed_at, item.code))
    times = sorted({item.observed_at for item in ordered})
    best = None
    for cut in times[1:]:
        early = [item for item in ordered if item.observed_at < cut]
        late = [item for item in ordered if item.observed_at >= cut]
        balance = abs(len(early) - len(late))
        if best is None or balance < best[0]:
            best = (balance, cut, early, late)
    return (best[1], best[2], best[3]) if best else (None, [], [])


def _apply(calibration, early, late):
    baseline = calibration.baseline_band
    early_scores = [_score(item) for item in early]
    early_labels = [bool(item.actual_priority) for item in early]
    late_scores = [_score(item) for item in late]
    late_labels = [bool(item.actual_priority) for item in late]
    candidate, selected = select_band(early_scores, early_labels, baseline)
    calibration.selection_rows = len(early)
    calibration.selection_priority = selected['priorities']
    calibration.blind_rows = len(late)
    calibration.blind_priority = sum(1 for label in late_labels if label)
    calibration.completed_at = utc_now()
    if candidate is None:
        calibration.candidate_band = None
        calibration.current_referrals = calibration.candidate_referrals = None
        calibration.current_missed = calibration.candidate_missed = None
        calibration.state = CalibrationState.REJECTED
        calibration.verdict = '前段没有可收窄且不增加漏放比例的带宽'
        return calibration
    current = stats(late_scores, late_labels, baseline)
    alternative = stats(late_scores, late_labels, candidate)
    calibration.candidate_band = candidate
    calibration.current_referrals = current['referrals']
    calibration.candidate_referrals = alternative['referrals']
    calibration.current_missed = current['missed']
    calibration.candidate_missed = alternative['missed']
    fewer = alternative['referrals'] < current['referrals']
    not_worse = alternative['miss_ratio'] <= current['miss_ratio'] + EPSILON
    calibration.state = CalibrationState.PASSED if fewer and not_worse else CalibrationState.REJECTED
    calibration.verdict = (
        f'前段选择 {candidate:.2f}；后段盲测送审 {alternative["referrals"]} 比 {current["referrals"]}，'
        f'漏放优先案 {alternative["missed"]}/{alternative["priorities"]} 对 {current["missed"]}/{current["priorities"]}。'
        + ('通过，可提交启用。' if fewer and not_worse else '未通过：须少送审且漏放比例不升。')
    )
    return calibration


def ensure_policy(db: DatabaseSession):
    """Create the active-band row once at startup so first-use reports never race to insert it."""
    if db.get(BandPolicy, 1) is None:
        db.add(BandPolicy(id=1, band=DEFAULT_BAND))
        db.flush()


def _next_code(db):
    codes = db.scalars(select(BandCalibration.code)).all()
    highest = max((int(code.split('-')[-1]) for code in codes if code.split('-')[-1].isdigit()), default=0)
    return f'BC-{highest + 1:03d}'


def _frozen(calibration, db):
    samples = db.scalars(select(CalibrationSample).where(
        CalibrationSample.calibration_code == calibration.code
    ).order_by(CalibrationSample.observed_at, CalibrationSample.incident_code)).all()
    return ([sample for sample in samples if sample.segment == 'selection'],
            [sample for sample in samples if sample.segment == 'blind'])


def create(db: DatabaseSession, actor: str):
    """Snapshot today's labelled cases and run the selection/blind evaluation."""
    cutoff = utc_now()
    baseline = current_band(db, lock=True)
    records = [record for record in db.scalars(select(DelayIncident).where(
        DelayIncident.actual_priority.is_not(None),
        DelayIncident.labelled_at.is_not(None),
        DelayIncident.labelled_at <= cutoff,
        DelayIncident.result.is_not(None),
    ).order_by(DelayIncident.observed_at, DelayIncident.code)) if _score(record) is not None]
    calibration = BandCalibration(
        code=_next_code(db), state=CalibrationState.EVALUATING, baseline_band=baseline,
        sample_cutoff=cutoff, created_by=actor, created_at=utc_now(),
    )
    if len(records) < 2:
        calibration.state = CalibrationState.REJECTED
        calibration.verdict = '已标注且完成分类的案件不足，无法按观测时间分为两段'
        calibration.completed_at = utc_now()
        db.add(calibration)
        audit(db, actor, calibration.code, 'CALIBRATION_REJECTED', calibration.verdict)
        return calibration
    split_at, early, late = _split(records)
    if split_at is None or not early or not late:
        calibration.state = CalibrationState.REJECTED
        calibration.verdict = '全部标注观测在同一时刻，无法按观测时间分段'
        calibration.completed_at = utc_now()
        db.add(calibration)
        audit(db, actor, calibration.code, 'CALIBRATION_REJECTED', calibration.verdict)
        return calibration
    calibration.split_at = split_at
    db.add(calibration)
    for segment, group in (('selection', early), ('blind', late)):
        for record in group:
            db.add(CalibrationSample(
                calibration_code=calibration.code, incident_code=record.code,
                observed_at=record.observed_at, segment=segment, score=_score(record),
                band=record.band, actual_priority=bool(record.actual_priority),
            ))
    db.flush()
    return _evaluate(db, actor, calibration)


def _evaluate(db, actor, calibration):
    early, late = _frozen(calibration, db)
    if not early or not late:
        calibration.state = CalibrationState.ERROR
        calibration.error = '样本快照不完整，无法分段'
        calibration.attempts += 1
        audit(db, actor, calibration.code, 'CALIBRATION_FAILED', calibration.error)
        return calibration
    try:
        _apply(calibration, early, late)
    except Exception as exc:  # keep the frozen sample for retry rather than opening a new evaluation
        calibration.state = CalibrationState.ERROR
        calibration.error = str(exc)[:180]
        calibration.attempts += 1
        audit(db, actor, calibration.code, 'CALIBRATION_FAILED', calibration.error)
        return calibration
    calibration.error = None
    calibration.attempts += 1
    calibration.completed_at = utc_now()
    audit(db, actor, calibration.code, f'CALIBRATION_{str(calibration.state).upper()}', calibration.verdict)
    return calibration


def retry(db: DatabaseSession, actor: str, code: str):
    """Re-run a failed evaluation on its original frozen sample; never records a second evaluation."""
    calibration = find(db, BandCalibration, code, lock=True)
    if calibration.state != CalibrationState.ERROR:
        raise Rejected('CALIBRATION_RETRY_STATE', '只有评估失败的记录可以重试', code)
    calibration.state = CalibrationState.EVALUATING
    audit(db, actor, code, 'CALIBRATION_REQUEUED', f'复用原样本 {calibration.sample_cutoff:%m-%d %H:%M}')
    return _evaluate(db, actor, calibration)


def recompare(db: DatabaseSession, actor: str, calibration: BandCalibration):
    """Re-run a frozen sample against the now-active band."""
    calibration.baseline_band = current_band(db)
    calibration.candidate_band = None
    return _evaluate(db, actor, calibration)


def activate(db: DatabaseSession, actor: str, code: str):
    calibration = find(db, BandCalibration, code, lock=True)
    if calibration.state == CalibrationState.ACTIVATED:
        return calibration
    if calibration.state != CalibrationState.PASSED:
        raise Rejected('CALIBRATION_NOT_PASSED', '只有盲测通过且尚未启用的评估可以启用', code)
    policy = db.get(BandPolicy, 1, with_for_update=True)
    if policy is None:
        policy = BandPolicy(id=1, band=DEFAULT_BAND)
        db.add(policy)
        db.flush()
    if calibration.baseline_band != policy.band:
        # Qualified under an older band: it must beat the band now in force before activation.
        # Re-test on the frozen sample at the new baseline; only a fresh pass may proceed.
        recompare(db, actor, calibration)
        if calibration.state != CalibrationState.PASSED:
            return calibration
    policy.band = calibration.candidate_band
    calibration.state = CalibrationState.ACTIVATED
    calibration.activated_at = utc_now()
    audit(db, actor, code, 'CALIBRATION_ACTIVATED',
          f'{calibration.baseline_band:.2f} → {calibration.candidate_band:.2f}')
    # A second qualified submission stays unactivated: it is re-tested against the newly active band.
    others = db.scalars(select(BandCalibration).where(
        BandCalibration.code != code,
        BandCalibration.state == CalibrationState.PASSED,
    )).all()
    for other in others:
        recompare(db, actor, other)
    return calibration
