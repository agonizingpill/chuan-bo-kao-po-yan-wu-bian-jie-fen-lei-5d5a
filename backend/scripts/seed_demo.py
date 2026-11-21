from datetime import timedelta

from sqlalchemy import func, select

from src.calibration import ensure_policy
from src.classifier import classify
from src.database import Session
from src.incidents import report
from src.models import (Audit, Berth, BerthRequest, DelayClass, DelayIncident, DispatchReview, IncidentState, Role,
                        TideWindow, Tug, TugAssignment, User)
from src.planning import add_request, add_window, confirm, reserve_tug
from src.schemas import IncidentInput, RequestInput, TideInput
from src.security import password_hash

DEMO_PASSWORD = 'Harbor1074!'


def historical(db):
    """Early-shift cases already closed and labelled, so the calibration page has something to evaluate."""
    if db.scalar(select(func.count()).select_from(DelayIncident).where(DelayIncident.actual_priority.is_not(None))):
        return
    for code, berth, start, end, draft in [('TW-092', 'B09', '05:30', '07:30', 13.0), ('TW-122', 'B12', '05:00', '09:00', 13.8)]:
        if not db.get(TideWindow, code):
            add_window(db, 'supervisor', TideInput(code=code, berth_code=berth,
                       starts_at=f'2026-11-21T{start}', ends_at=f'2026-11-21T{end}',
                       max_draft_m=draft, bulletin='TIDE-EAST-1121-AM.pdf'))
    db.flush()
    # Non-overlapping per berth and per tug so confirm() accepts each plan. Features span the boundary:
    # early small-delay cases with ample tide reserve on the left, late high-wind/wait cases near the window end.
    plan = [
        ('BR-390', '顺达 11', 'B09', 'TW-092', '05:40', '05:52', 7, 4, 'TG12'),
        ('BR-391', '海盛 8', 'B09', 'TW-092', '06:30', '06:56', 17, 15, 'TG18'),
        ('BR-392', '东茂 5', 'B12', 'TW-122', '05:00', '05:08', 4, 2, 'TG23'),
        ('BR-393', '远航 21', 'B12', 'TW-122', '05:45', '05:58', 8, 5, 'TG23'),
        ('BR-394', '金桥 9', 'B12', 'TW-122', '06:30', '06:48', 12, 9, 'TG23'),
        ('BR-395', '海通 33', 'B12', 'TW-122', '07:15', '07:38', 16, 13, 'TG23'),
        ('BR-396', '临港 6', 'B12', 'TW-122', '08:00', '08:26', 19, 16, 'TG23'),
    ]
    for index, (code, vessel, berth, window, eta, observed, wind, wait, tug) in enumerate(plan):
        if not db.get(BerthRequest, code):
            add_request(db, 'operator', RequestInput(code=code, vessel=vessel, berth_code=berth, window_code=window,
                        eta=f'2026-11-21T{eta}', maneuver_minutes=40, draft_m=12.0))
            db.flush()
            reserve_tug(db, 'operator', code, tug)
            confirm(db, 'supervisor', code)
        incident_code = f'DL-{code.split("-")[1]}'
        if db.get(DelayIncident, incident_code):
            continue
        incident = report(db, 'operator', IncidentInput(
            code=incident_code, request_code=code, radio_reference=f'VHF-09/{observed.replace(":", "")}-{index + 1:02d}',
            observed_at=f'2026-11-21T{observed}', wind_kn=wind, tug_wait_min=wait,
            event_note='早班引航通报局部延误，值班主管已复核并完成交接。'))
        db.flush()
        result = classify(incident.features, incident.input_digest, incident.band)
        incident.result = result
        incident.state = IncidentState.CLOSED
        incident.attempts = 1
        incident.decision = DelayClass.PRIORITY if result['score'] > 0 else DelayClass.ROUTINE
        incident.decided_by = 'supervisor'
        incident.decided_at = incident.observed_at
        # Ground truth as recorded at close: mostly agrees with the model, a couple of near-band corrections.
        incident.actual_priority = result['score'] > -0.05
        incident.labelled_by = 'supervisor'
        incident.labelled_at = incident.observed_at + timedelta(hours=1)
    db.flush()



def seed():
    with Session.begin() as db:
        ensure_policy(db)
        for username, role in [('operator', Role.OPERATOR), ('supervisor', Role.SUPERVISOR), ('viewer', Role.VIEWER)]:
            if not db.get(User, username):
                db.add(User(username=username, role=role, password_hash=password_hash(DEMO_PASSWORD)))
        for code, label in [('B07', '东七码头'), ('B09', '东九散货泊位'), ('B12', '南十二泊位')]:
            if not db.get(Berth, code):
                db.add(Berth(code=code, label=label))
        for code, label, strength in [('TG12', '海勤 12', 58), ('TG18', '海勤 18', 65), ('TG23', '远协 23', 72)]:
            if not db.get(Tug, code):
                db.add(Tug(code=code, label=label, bollard_pull_t=strength))
        for code, berth, start, end, draft in [
            ('TW-071', 'B07', '07:45', '09:10', 11.8), ('TW-072', 'B07', '09:10', '10:45', 13.2),
            ('TW-091', 'B09', '08:30', '11:40', 12.5), ('TW-121', 'B12', '07:25', '12:10', 13.5),
        ]:
            if not db.get(TideWindow, code):
                add_window(db, 'supervisor', TideInput(code=code, berth_code=berth,
                           starts_at=f'2026-11-21T{start}', ends_at=f'2026-11-21T{end}',
                           max_draft_m=draft, bulletin='TIDE-EAST-1121-AM.pdf'))
        db.flush()
        for code, vessel, berth, window, eta, minutes, draft in [
            ('BR-410', '海岚 27', 'B07', 'TW-071', '08:50', 45, 12.4),
            ('BR-411', '临海 308', 'B09', 'TW-091', '09:25', 55, 11.8),
            ('BR-412', '松航 16', 'B12', 'TW-121', '11:00', 45, 12.5),
            ('BR-413', '远桥 75', 'B07', 'TW-072', '09:30', 50, 11.9),
        ]:
            if db.get(BerthRequest, code):
                continue
            add_request(db, 'operator', RequestInput(code=code, vessel=vessel, berth_code=berth, window_code=window,
                        eta=f'2026-11-21T{eta}', maneuver_minutes=minutes, draft_m=draft))
            db.flush()
            if code in ('BR-411', 'BR-413'):
                reserve_tug(db, 'operator', code, 'TG12' if code == 'BR-411' else 'TG18')
                confirm(db, 'supervisor', code)
        if not db.get(DelayIncident, 'DL-413'):
            report(db, 'operator', IncidentInput(code='DL-413', request_code='BR-413',
                   radio_reference='VHF-12/0930-19', observed_at='2026-11-21T09:48', wind_kn=14,
                   tug_wait_min=12, event_note='引航员通报拖轮还在回转区，现场请示是否申请待命。'))
        historical(db)
        db.flush()
        tables = (User, Berth, Tug, TideWindow, BerthRequest, TugAssignment, DelayIncident, DispatchReview, Audit)
        return {table.__tablename__: db.scalar(select(func.count()).select_from(table)) for table in tables}


if __name__ == '__main__':
    print(seed())
