from fastapi import APIRouter,Depends,HTTPException
from sqlalchemy import select
from .db import db_session
from .auth import current,writers
from .models import Layout
from .schemas import LiftLimitInput
from .lift_models import LayoutLiftCheck,RunLiftCheck
from .run_models import TunnelRun
from .services import pack
from . import lift_service as lift
router=APIRouter()

@router.get('/lift-checks')
def all_checks(s=Depends(db_session),u=Depends(current)):
    layout_checks={x.layout_id:pack(x) for x in s.scalars(select(LayoutLiftCheck))}
    latest={}
    for row in s.scalars(select(RunLiftCheck).order_by(RunLiftCheck.sequence)):
        latest[row.run_id]=pack(row)
    return {'layouts':layout_checks,'runs':latest}

@router.get('/layouts/{lid}/lift-check')
def get_check(lid:int,s=Depends(db_session),u=Depends(current)):
    l=s.get(Layout,lid)
    if not l:raise HTTPException(404,'布置方案不存在')
    return lift.view(s,l)

@router.post('/layouts/{lid}/lift-check')
def compute_check(lid:int,d:LiftLimitInput,s=Depends(db_session),u=Depends(writers)):
    l=s.scalar(select(Layout).where(Layout.id==lid).with_for_update())
    if not l:raise HTTPException(404,'布置方案不存在')
    x=lift.dispatch(s,l,d.limit_n,u);s.commit();return x

@router.get('/tunnel-runs/{id}/lift-check')
def get_run_check(id:int,s=Depends(db_session),u=Depends(current)):
    r=s.get(TunnelRun,id)
    if not r:raise HTTPException(404,'试验计划不存在')
    row=s.scalar(select(RunLiftCheck).where(RunLiftCheck.run_id==id).order_by(RunLiftCheck.sequence.desc()))
    return {'run_id':id,'latest':pack(row) if row else None}
