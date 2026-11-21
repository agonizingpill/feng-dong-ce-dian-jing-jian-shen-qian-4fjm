import os,json,logging
from pathlib import Path
import redis
from sqlalchemy import select
from .db import Session
from .models import Layout
from .lift_models import LayoutLiftCheck
from .selection import calculate
from .lift_service import recompute as recompute_lift,view as lift_view
r=redis.Redis.from_url(os.environ['REDIS_URL'],decode_responses=True);Path('/artifacts').mkdir(exist_ok=True)
while True:
    Path('/tmp/worker-heartbeat').touch();entry=r.blpop('tunnel-selection',timeout=2)
    if not entry:continue
    with Session() as s:
        l=s.get(Layout,int(entry[1]))
        if not l or l.status!='calculating':continue
        try:
            calculate(s,l)
            if s.scalar(select(LayoutLiftCheck.id).where(LayoutLiftCheck.layout_id==l.id)):
                recompute_lift(s,l)
                Path(f'/artifacts/layout-{l.id}-lift.json').write_text(json.dumps(lift_view(s,l),ensure_ascii=False))
            l.status='ready';s.commit();Path(f'/artifacts/layout-{l.id}.json').write_text(json.dumps(l.evidence,ensure_ascii=False))
        except Exception as ex:
            s.rollback();l=s.get(Layout,int(entry[1]));l.status='failed';l.evidence={'error':str(ex)};s.commit();logging.exception('selection failed')
