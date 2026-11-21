import os
import pytest
from sqlalchemy import select
from src.db import Session
from src.models import Layout,LayoutProbe,Probe
from src.lift_models import LayoutLiftCheck,RunLiftCheck
from src import lift_service
from tests.test_lift_flow import client,token,cert,make_layout,probe_ids,recalc  # noqa: F401

def signed_layout(c,limit='10000',codes=('P01','P05','P09')):
    p=probe_ids(c);lid=make_layout(c,limit=limit,probes=[p[x] for x in codes])
    h=token(c);r=c.post(f'/layouts/{lid}/sign',headers=h);assert r.status_code==200,r.text
    return lid

def mount_for(c,lid):
    h=token(c)
    r=c.post('/model-mounts',headers=h,json={'code':'MT-'+os.urandom(3).hex(),'layout_id':lid,'mass_kg':5,
        'area_m2':.2,'torque_nm':45,'load_limit':2000});assert r.status_code==200,r.text
    mid=r.json()['id'];assert c.post(f'/model-mounts/{mid}/accept',headers=h).status_code==200
    return mid

def planned_run(c,lid,code=None):
    h=token(c);mid=mount_for(c,lid)
    r=c.post('/tunnel-runs',headers=h,json={'layout_id':lid,'code':code or 'RUN-T-'+os.urandom(3).hex(),
        'speed':30,'target_angle':8,'checklist':{'air_path_clear':True,'emergency_stop_tested':True,'model_secured':True}})
    assert r.status_code==200,r.text;rid=r.json()['id']
    assert c.post('/run-mounts',headers=h,json={'run_id':rid,'mount_id':mid}).status_code==200
    return rid

def test_start_requires_signed_lift_basis_and_snapshots_two_bases(client):
    lid=signed_layout(client)
    rid=planned_run(client,lid)
    h=token(client)
    r=client.post(f'/tunnel-runs/{rid}/start',headers=h);assert r.status_code==200,r.text
    snap=r.json()['snapshot']
    assert snap['lift_check']['layout_id']==lid
    assert snap['lift_check']['certificates']
    assert snap['lift_check']['recomputed']['holdout_rows']==24
    got=client.get(f'/tunnel-runs/{rid}/lift-check',headers=h).json()['latest']
    assert got['status']=='ready' and got['sequence']==1
    assert got['evidence']['signed_basis']['limit_n']==10000
    cert_ids={c['id'] for c in snap['lift_check']['certificates']}
    assert set(got['certificate_ids'])==cert_ids
    assert [p['certificate_code'] for p in snap['lift_check']['recomputed']['selected_probes']]==[
        c['code'] for c in snap['lift_check']['certificates']]

def test_new_certificate_push_over_limit_blocks_start(client):
    p=probe_ids(client)
    # 先算出该布置的原始偏差，用刚好等于它的上限签署，随后换发更差但仍可签发的证书
    lid=make_layout(client,limit='10000',probes=[p['P01'],p['P05'],p['P09']])
    h=token(client)
    res=client.get(f'/layouts/{lid}/lift-check',headers=h).json()['check']['result']
    tight=round(res['max_abs_deviation_n'],2)+0.01
    r=client.post(f'/layouts/{lid}/lift-check',headers=h,json={'limit_n':tight});assert r.status_code==200
    assert r.json()['pass_ready'] is True
    assert client.post(f'/layouts/{lid}/sign',headers=h).status_code==200
    rid=planned_run(client,lid)
    assert client.post(f'/tunnel-runs/{rid}/start',headers=h).status_code==200
    rid2=planned_run(client,lid)
    cert(client,'P09',zero=0.45,span=0.9,code='CAL-WORSE-P09')
    r=client.post(f'/tunnel-runs/{rid2}/start',headers=h)
    assert r.status_code==409 and '退回' in r.json()['detail']
    latest=client.get(f'/tunnel-runs/{rid2}/lift-check',headers=h).json()['latest']
    assert latest['status']=='failed' and latest['result']['within_limit'] is False
    assert latest['evidence']['certificates']
    # 再换回合格证书后可以开车
    cert(client,'P09',zero=0.05,span=0.2,code='CAL-GOOD-P09-2')
    r=client.post(f'/tunnel-runs/{rid2}/start',headers=h);assert r.status_code==200,r.text
    assert r.json()['snapshot']['lift_check']['within_limit'] is True
    hist=client.get(f'/tunnel-runs/{rid2}/lift-check',headers=h).json()['latest']
    assert hist['sequence']==2 and hist['status']=='ready'

def test_limit_change_after_signing_rejected_and_probe_change_resets(client):
    p=probe_ids(client);lid=signed_layout(client)
    h=token(client)
    assert client.post(f'/layouts/{lid}/lift-check',headers=h,json={'limit_n':200}).status_code==409
    assert client.put(f'/layouts/{lid}/probes',headers=h,json={'probe_ids':[p['P01'],p['P05'],p['P09']]}).status_code==409

def test_legacy_signed_layout_keeps_signature_but_blocks_start(client):
    p=probe_ids(client)
    lid=make_layout(client,limit='10000',probes=[p['P01'],p['P05'],p['P09']])
    h=token(client);assert client.post(f'/layouts/{lid}/sign',headers=h).status_code==200
    with Session() as s:
        # 模拟升级前旧库：已签布置，但没有升力偏差记录
        s.delete(s.scalar(select(LayoutLiftCheck).where(LayoutLiftCheck.layout_id==lid)))
        s.commit()
    rid=planned_run(client,lid)
    r=client.post(f'/tunnel-runs/{rid}/start',headers=h)
    assert r.status_code==409 and '旧数据升级' in r.json()['detail']
    with Session() as s:
        l=s.get(Layout,lid);assert l.status=='signed' and l.signed_by=='admin@yiheng.demo'
        x=s.scalar(select(LayoutLiftCheck).where(LayoutLiftCheck.layout_id==lid))
        assert x and x.signed_by=='admin@yiheng.demo' and x.synthesized is True
    # 另签新布置才能开车
    lid2=signed_layout(client)
    rid2=planned_run(client,lid2)
    assert client.post(f'/tunnel-runs/{rid2}/start',headers=h).status_code==200

def test_new_certificate_between_compute_and_sign_blocks_sign(client):
    p=probe_ids(client);h=token(client)
    lid=make_layout(client,limit='10000',probes=[p['P01'],p['P05'],p['P09']])
    res=client.get(f'/layouts/{lid}/lift-check',headers=h).json()['check']['result']
    tight=round(res['max_abs_deviation_n'],2)+0.01
    assert client.post(f'/layouts/{lid}/lift-check',headers=h,json={'limit_n':tight}).json()['pass_ready'] is True
    cert(client,'P09',zero=0.45,span=0.9,code='CAL-SWAP-P09')
    r=client.post(f'/layouts/{lid}/sign',headers=h)
    assert r.status_code==409 and '最新证书' in r.json()['detail']
    v=client.get(f'/layouts/{lid}/lift-check',headers=h).json()['check']
    assert v['signed_by'] is None and v['pass_ready'] is False
    with Session() as s:
        l=s.get(Layout,lid);assert l.status=='ready'

def test_run_recheck_certificate_set_is_single_transaction(client):
    p=probe_ids(client);lid=signed_layout(client)
    h=token(client)
    # 换证与开车交错：先登记一张新证书但不签发，复核仍使用最近已签证书
    c=cert(client,'P09',zero=0.3,span=0.4,code='CAL-DRAFT-P09',issue=False)
    rid=planned_run(client,lid)
    r=client.post(f'/tunnel-runs/{rid}/start',headers=h)
    assert r.status_code==200,r.text
    snap=r.json()['snapshot']
    codes=[c['code'] for c in snap['certificates']]
    assert 'CAL-DRAFT-P09' not in codes
    assert codes==[p['certificate_code'] for p in snap['lift_check']['recomputed']['selected_probes']]
    assert {c['id'] for c in snap['certificates']}==set(snap['lift_check']['prerequisites']['certificate_ids'])

def test_failed_recheck_keeps_certificate_evidence(client):
    p=probe_ids(client);h=token(client)
    cert(client,'P09',zero=0.05,span=0.2,code='CAL-EVIDENCE-BASE-P09')
    lid=make_layout(client,limit='10000',probes=[p['P01'],p['P05'],p['P09']])
    res=client.get(f'/layouts/{lid}/lift-check',headers=h).json()['check']['result']
    tight=round(res['max_abs_deviation_n'],2)+0.01
    assert client.post(f'/layouts/{lid}/lift-check',headers=h,json={'limit_n':tight}).json()['pass_ready'] is True
    assert client.post(f'/layouts/{lid}/sign',headers=h).status_code==200
    rid=planned_run(client,lid)
    cert(client,'P09',zero=0.45,span=0.9,code='CAL-BAD-EVIDENCE-P09')
    assert client.post(f'/tunnel-runs/{rid}/start',headers=h).status_code==409
    ev=client.get(f'/tunnel-runs/{rid}/lift-check',headers=h).json()['latest']
    assert ev['status']=='failed' and ev['evidence']['certificates']
    assert ev['evidence']['recomputed']['within_limit'] is False
    assert ev['evidence']['signed_basis']['max_abs_deviation_n'] is not None
    assert ev['certificate_ids']==ev['prerequisites']['certificate_ids']
