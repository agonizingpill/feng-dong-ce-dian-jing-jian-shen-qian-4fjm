import os
from sqlalchemy import select
from src.db import Session
from src.models import Layout
from src.calibration_models import CalibrationCertificate
from src import lift_service
from tests.conftest import client,token,cert,probe_ids  # noqa: F401

def make_layout(c,limit='50',probes=None):
    h=token(c);r=c.post('/layouts',headers=h,json={'code':'LY-T-'+os.urandom(3).hex(),'max_channels':3,'limit_n':float(limit)})
    assert r.status_code==200,r.text;lid=r.json()['id']
    with Session() as s:
        l=s.get(Layout,lid);l.status='ready';s.commit()   # 模拟 worker 完成测点计算
    if probes:
        r=c.put(f'/layouts/{lid}/probes',headers=h,json={'probe_ids':probes});assert r.status_code==200,r.text
    recalc(c,lid)
    return lid

def recalc(c,lid):
    from sqlalchemy import select
    from src.lift_models import LayoutLiftCheck
    from src.services import source_checksum
    with Session() as s:
        l=s.get(Layout,lid)
        assert s.scalar(select(LayoutLiftCheck.id).where(LayoutLiftCheck.layout_id==lid))
        l.status='ready';l.source_checksum=source_checksum(s)
        lift_service.recompute(s,l);s.commit()

def test_layout_carries_limit_and_computes(client):
    p=probe_ids(client);lid=make_layout(client,limit='500',probes=[p['P01'],p['P05'],p['P09']])
    h=token(client);v=client.get(f'/layouts/{lid}/lift-check',headers=h).json()
    assert v['check']['limit_n']==500
    assert v['check']['status']=='computed'
    r=v['check']['result']
    assert r['holdout_rows']==24 and r['train_rows']==96
    assert len(r['holdout'])==24
    assert r['max_abs_deviation_n']>=r['max_ridge_residual_n']

def test_layout_without_limit_rejected(client):
    h=token(client)
    r=client.post('/layouts',headers=h,json={'code':'LY-NOLIMIT','max_channels':3})
    assert r.status_code==422
    r=client.post('/layouts',headers=h,json={'code':'LY-ZEROLIMIT','max_channels':3,'limit_n':0})
    assert r.status_code==422

def test_sign_requires_limit_met(client):
    p=probe_ids(client);lid=make_layout(client,limit='0.001',probes=[p['P01'],p['P05'],p['P09']])
    h=token(client)
    v=client.get(f'/layouts/{lid}/lift-check',headers=h).json()['check']
    assert v['status']=='computed' and v['pass_ready'] is False
    r=client.post(f'/layouts/{lid}/sign',headers=h)
    assert r.status_code==409 and '超出上限' in r.json()['detail']
    assert client.get('/layouts',headers=h).json()
    assert any(x['id']==lid and x['status']=='ready' for x in client.get('/layouts',headers=h).json())

def test_sign_records_basis_and_freezes(client):
    p=probe_ids(client);lid=make_layout(client,limit='10000',probes=[p['P01'],p['P05'],p['P09']])
    h=token(client)
    r=client.post(f'/layouts/{lid}/sign',headers=h);assert r.status_code==200,r.text
    sig=r.json()['lift_check']
    assert sig['signed_by']=='admin@yiheng.demo'
    assert sig['signature']['limit_n']==10000
    assert len(sig['signature']['holdout_sweep_codes'])==24
    assert sig['signature']['probe_ids']==sorted([p['P01'],p['P05'],p['P09']])
    assert sig['signature']['certificate_codes']==['CAL-BASE-P01','CAL-BASE-P05','CAL-BASE-P09']
    r=client.post(f'/layouts/{lid}/lift-check',headers=h,json={'limit_n':5000})
    assert r.status_code==409

def test_changing_limit_recomputes_and_clears_pass(client):
    p=probe_ids(client);lid=make_layout(client,limit='10000',probes=[p['P01'],p['P05'],p['P09']])
    h=token(client)
    r=client.post(f'/layouts/{lid}/lift-check',headers=h,json={'limit_n':0.0001});assert r.status_code==200
    assert r.json()['sequence']==2 and r.json()['pass_ready'] is False

def test_expired_certificate_blocks_compute_and_sign(client):
    p=probe_ids(client)
    h=token(client)
    # 先以有效日期签发，再模拟时间推移使其过期（负责人签发时不允许已过期证书）
    r=client.post('/calibration-certificates',headers=h,json={'probe_id':p['P09'],'code':'CAL-EXPIRED-P09',
        'zero_error':0.1,'span_error':0.2,'valid_until':'2027-06-30'});assert r.status_code==200
    cid=r.json()['id'];assert client.post(f'/calibration-certificates/{cid}/issue',headers=h).status_code==200
    expired=client.post('/calibration-certificates',headers=h,json={'probe_id':p['P09'],'code':'CAL-ALREADY-EXPIRED-P09',
        'zero_error':0.1,'span_error':0.2,'valid_until':'2020-01-01'}).json()['id']
    assert client.post(f'/calibration-certificates/{expired}/issue',headers=h).status_code==409
    from datetime import datetime,timezone
    with Session() as s:
        c=s.get(CalibrationCertificate,cid);c.valid_until=datetime(2020,1,1).date();s.commit()
    lid=make_layout(client,limit='10000',probes=[p['P01'],p['P05'],p['P09']])
    v=client.get(f'/layouts/{lid}/lift-check',headers=h).json()['check']
    assert v['status']=='failed' and v['synthesized'] is True
    assert any('P09' in x for x in v['prerequisites']['problems'])
    r=client.post(f'/layouts/{lid}/sign',headers=h)
    assert r.status_code==409
    # 再签一张新有效证书后恢复
    cert(client,'P09',zero=0.05,span=0.2,code='CAL-FIX-P09')
    lid2=make_layout(client,limit='10000',probes=[p['P01'],p['P05'],p['P09']])
    v=client.get(f'/layouts/{lid2}/lift-check',headers=h).json()['check']
    assert v['status']=='computed' and v['synthesized'] is False
    assert client.post(f'/layouts/{lid2}/sign',headers=h).status_code==200
