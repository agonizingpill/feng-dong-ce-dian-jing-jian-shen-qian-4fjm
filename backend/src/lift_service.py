from copy import deepcopy
from datetime import datetime,timezone,date
import numpy as np
from fastapi import HTTPException
from sqlalchemy import select
from sklearn.linear_model import Ridge
from .models import Probe,Sweep,Pressure,Layout
from .lift_models import LayoutLiftCheck,RunLiftCheck
from .calibration_service import locked,current_certificate,view as certificate_view
from .services import pack,selected,audit,source_checksum
from .selection import holdout_split

Z_SIGMA=4.0
FORMULA=('worst_abs = |ridge_holdout_residual| + sum_k(|g_k| * |zero_error_k|) + '
         'sum_k(|g_k| * |p_k-mean_k| * |span_error_k|/100) ; '
         'g_k = w_k/sigma_k 为升力对原始压力 Pa 的灵敏系数（w_k 为训练集标准化空间 Ridge 系数，'
         'mean_k、sigma_k 取训练集），|p_k-mean_k| 超过 4*sigma_k 的留出样本按 4*sigma_k 计以排除外插；'
         '零点与跨度误差按同向叠加，取最不利留出样本')

def _now():return datetime.now(timezone.utc).isoformat()

def _matrices(s):
    probes=s.scalars(select(Probe).order_by(Probe.id)).all()
    rows=s.scalars(select(Sweep).order_by(Sweep.id)).all()
    if len(rows)<40:raise HTTPException(409,'至少40组完整扫角才可核算升力偏差')
    values={(p.sweep_id,p.probe_id):p.pascal for p in s.scalars(select(Pressure))}
    try:X=np.array([[values[(r.id,p.id)] for p in probes]for r in rows]);y=np.array([r.lift for r in rows],dtype=float)
    except KeyError:raise HTTPException(409,'扫角存在缺测点，不能核算升力偏差')
    train,test=holdout_split(len(rows))
    return probes,rows,X,y,train,test

def _cert_of(s,p):
    try:return current_certificate(s,p),None
    except HTTPException as e:return None,str(e.detail)

def _layout_check(s,lid):
    return s.scalar(select(LayoutLiftCheck).where(LayoutLiftCheck.layout_id==lid).with_for_update())

def _layouts_check(s,lid):
    return s.scalar(select(LayoutLiftCheck).where(LayoutLiftCheck.layout_id==lid))

def summary(s,l):
    x=_layouts_check(s,l.id)
    return pack(x) if x else None

def resolve_certificates(s,ids,lock=True):
    """在一个事务里锁定测点并取各自最新签发证书；核算与留档共用这一组证书。"""
    items=[];certs={}
    for pid in sorted(ids):
        p=locked(s,Probe,pid) if lock else s.get(Probe,pid)
        if not p:raise HTTPException(404,'布置测点或校准记录不存在')
        cert,problem=_cert_of(s,p)
        if cert:certs[pid]=cert
        items.append({'probe_id':p.id,'code':p.code,'certificate_code':cert.code if cert else None,
                      'valid_until':cert.valid_until.isoformat() if cert else None,
                      'zero_error':cert.zero_error if cert else None,'span_error':cert.span_error if cert else None,
                      'status':'valid' if cert else 'invalid','problem':problem})
    problems=[f"{x['code']}：{x['problem']}" for x in items if x['status']!='valid']
    meta={'checked_at':_now(),'items':items,'problems':problems,'synthesized':bool(problems),
          'source_checksum':source_checksum(s),'certificate_ids':[c.id for c in certs.values()]}
    return meta,certs

def compute(s,l,limit,certs):
    ids=selected(s,l.id)
    if not ids:raise HTTPException(409,'方案尚未选点，不能核算升力偏差')
    if set(certs)!=set(ids):raise HTTPException(409,'核算证书与所选测点不一致')
    probes_all=s.scalars(select(Probe).order_by(Probe.id)).all()
    col=[i for i,p in enumerate(probes_all) if p.id in ids]
    if len(col)!=len(ids):raise HTTPException(409,'布置测点已不存在，请重新计算方案')
    _,rows,X,y,train,test=_matrices(s)
    ordered=[p for p in probes_all if p.id in ids]
    zero=np.array([certs[p.id].zero_error for p in ordered],dtype=float)
    span=np.array([abs(certs[p.id].span_error)/100.0 for p in ordered],dtype=float)
    xtr=X[train][:,col];mu=xtr.mean(axis=0);sd=xtr.std(axis=0);sd[sd==0]=1.0
    model=Ridge(alpha=1.0).fit((xtr-mu)/sd,y[train])
    gain=np.asarray(model.coef_,dtype=float)/sd
    z=(X[test][:,col]-mu)/sd
    actual=X[test][:,col];span_pa=np.minimum(np.abs(actual-mu),Z_SIGMA*sd)
    base=model.predict(z);resid=np.abs(base-y[test])
    bias=float(np.sum(np.abs(gain)*zero))
    spread_row=(span_pa*(np.abs(gain)*span)).sum(axis=1)
    bound_row=resid+bias+spread_row
    worst=float(bound_row.max());worst_index=int(np.argmax(bound_row))
    holdout=[{'sweep':rows[i].code,'actual_lift_n':round(float(y[i]),3),'ridge_predicted_n':round(float(base[j]),3),
              'ridge_residual_n':round(float(base[j]-y[i]),3),'zero_bias_n':round(bias,6),
              'span_bound_n':round(float(spread_row[j]),6),'worst_abs_n':round(float(bound_row[j]),6)}
             for j,i in enumerate(test)]
    per_probe=[{'probe_id':p.id,'code':p.code,'zone':p.zone,'gain_n_per_pa':round(float(gain[k]),6),
                'zero_error_pa':certs[p.id].zero_error,'span_error_pct':certs[p.id].span_error,
                'mean_pa':round(float(mu[k]),4),'sigma_pa':round(float(sd[k]),4),
                'certificate_code':certs[p.id].code}
               for k,p in enumerate(ordered)]
    return {'limit_n':float(limit),'within_limit':worst<=limit,'max_abs_deviation_n':round(worst,4),
            'mean_abs_deviation_n':round(float(bound_row.mean()),4),
            'worst_holdout_sweep':rows[test[worst_index]].code,
            'bias_bound_n':round(bias,6),'spread_bound_n':round(float(spread_row.max()),6),
            'max_ridge_residual_n':round(float(resid.max()),4),
            'holdout_rows':int(len(test)),'train_rows':int(len(train)),'seed':42,'alpha':1.0,'z_sigma':Z_SIGMA,
            'formula':FORMULA,'holdout_sweep_codes':[rows[i].code for i in test],
            'train_sweep_codes':[rows[i].code for i in train],'selected_probe_ids':ids,
            'selected_probes':per_probe,'holdout':holdout,
            'source_checksum':source_checksum(s),'computed_at':_now()}

def dispatch(s,l,limit,u):
    if l.status=='signed':raise HTTPException(409,'已审签布置不能改升力偏差上限')
    if l.status!='ready':raise HTTPException(409,'仅计算完成的方案可设定升力偏差上限，请先回算测点方案')
    existing=_layout_check(s,l.id)
    if existing and existing.signed_by:raise HTTPException(409,'升力偏差已审签，不能重算覆盖')
    if not existing:
        x=LayoutLiftCheck(layout_id=l.id,limit_n=float(limit),sequence=1);s.add(x);s.flush()
    else:
        x=existing;x.limit_n=float(limit);x.sequence=x.sequence+1;x.result=None;x.error=None;x.pass_ready=False
    x.signature=None;x.signed_by=None;x.signed_at=None
    _compute_into(s,l,x)
    audit(s,u,f'核算升力偏差 {l.code} 上限{limit}N 最大{x.result["max_abs_deviation_n"] if x.result else "—"}N')
    return pack(x)

def begin(s,l,limit):
    """建布置时登记上限；worker 算完测点后紧接同一上限核算。"""
    x=LayoutLiftCheck(layout_id=l.id,limit_n=float(limit),sequence=0)
    s.add(x);s.flush();return x

def recompute(s,l,u=None):
    x=_layout_check(s,l.id)
    if not x:raise HTTPException(409,'布置尚未登记升力偏差上限')
    x.sequence=x.sequence+1;x.result=None;x.error=None;x.pass_ready=False
    _compute_into(s,l,x)
    if u:audit(s,u,f'重新核算升力偏差 {l.code} 上限{x.limit_n}N')
    return x

def _compute_into(s,l,x):
    ids=selected(s,l.id)
    if not ids:
        x.status='failed';x.pass_ready=False;x.synthesized=False;x.error='方案尚未选点，不能核算升力偏差';return
    meta,certs=resolve_certificates(s,ids)
    x.prerequisites=meta
    if meta['synthesized']:
        x.status='failed';x.result=None;x.pass_ready=False;x.synthesized=True
        x.error='证书前置条件未通过：'+'; '.join(meta['problems'])[:480];return
    try:result=compute(s,l,x.limit_n,certs)
    except HTTPException as e:
        x.status='failed';x.result=None;x.pass_ready=False;x.synthesized=False
        x.error=str(e.detail)[:500];x.prerequisites=None;return
    x.status='computed';x.pass_ready=bool(result['within_limit']);x.synthesized=False
    x.result=deepcopy(result);x.prerequisites=None
    x.signature=None
    x.signed_by=None;x.signed_at=None

def backfill(s):
    """旧数据升级：已签布置若没有升力偏差记录，保留原签名并标记为合成依据。"""
    created=[]
    for l in s.scalars(select(Layout).where(Layout.status=='signed')).all():
        if s.scalar(select(LayoutLiftCheck.id).where(LayoutLiftCheck.layout_id==l.id)):continue
        ids=selected(s,l.id)
        x=LayoutLiftCheck(layout_id=l.id,limit_n=None,sequence=0,status='failed',
                          pass_ready=False,synthesized=True,
                          error='旧数据升级：该布置签署时没有升力偏差核算依据，仅保留原签名，须另签新布置方可开车')
        x.prerequisites={'legacy':True,'probe_ids':ids,'problems':['缺少签署时的原模型留出核算依据']}
        x.signature={'legacy':True,'original_signed_by':l.signed_by,'original_source_checksum':l.source_checksum,
                     'probe_ids':ids,'note':'升级前签署，未留存原模型升力偏差依据'}
        x.signed_by=l.signed_by;x.signed_at=None
        s.add(x);created.append(l.code)
    s.flush()
    return created

def reset(s,l):
    x=s.scalar(select(LayoutLiftCheck).where(LayoutLiftCheck.layout_id==l.id).with_for_update())
    if x and x.signed_by:raise HTTPException(409,'升力偏差已审签，不能重新选点')
    if x:
        x.status='calculating';x.result=None;x.error=None;x.pass_ready=False;x.synthesized=False
        x.prerequisites=None;x.signature=None;x.signed_at=None

def ensure_ready(s,l):
    x=_layouts_check(s,l.id)
    if not x:raise HTTPException(409,'尚未设定升力偏差上限并核算')
    if x.status!='computed' or x.synthesized:raise HTTPException(409,'升力偏差核算未完成，不能审签布置')
    if not x.pass_ready:raise HTTPException(409,'升力偏差超出上限，不能审签布置')
    r=x.result
    if r['limit_n']!=x.limit_n or r['source_checksum']!=source_checksum(s):raise HTTPException(409,'升力偏差依据已变化，请重新核算')
    if set(r['selected_probe_ids'])!=set(selected(s,l.id)):raise HTTPException(409,'布置测点已变化，请重新核算升力偏差')
    return x

def sign_with_layout(s,l,u):
    x=_layout_check(s,l.id)
    if not x:raise HTTPException(409,'须先设定升力偏差上限并完成核算')
    if x.signed_by:raise HTTPException(409,'升力偏差已经审签，不得重复签署')
    ensure_ready(s,l)
    ids=selected(s,l.id)
    # 签署时按当下最新证书重算一次：签署依据、留档证书与判定必须是同一组证书
    meta,certs=resolve_certificates(s,ids)
    x.prerequisites=meta
    if meta['synthesized']:
        x.synthesized=True;x.error='证书复核未通过：'+'; '.join(meta['problems'])[:480]
        s.flush();s.commit();raise HTTPException(409,x.error)
    result=compute(s,l,x.limit_n,certs)
    x.result=deepcopy(result);x.pass_ready=bool(result['within_limit']);x.synthesized=False
    if not result['within_limit']:
        x.error=f'按最新证书重算升力偏差最大{result["max_abs_deviation_n"]}N，超过上限{x.limit_n}N'
        s.flush();s.commit();raise HTTPException(409,x.error)
    x.signed_by=u.email;x.signed_at=_now()
    x.signature={'limit_n':x.limit_n,'max_abs_deviation_n':result['max_abs_deviation_n'],
                 'mean_abs_deviation_n':result['mean_abs_deviation_n'],
                 'source_checksum':result['source_checksum'],'holdout_sweep_codes':result['holdout_sweep_codes'],
                 'train_sweep_codes':result['train_sweep_codes'],
                 'train_rows':result['train_rows'],'holdout_rows':result['holdout_rows'],
                 'alpha':result['alpha'],'z_sigma':result['z_sigma'],'formula':result['formula'],
                 'computed_at':result['computed_at'],
                 # 签下的原模型：灵敏系数与训练集均值/跨度，供日后复核比对
                 'model':[{'probe_id':p['probe_id'],'code':p['code'],'gain_n_per_pa':p['gain_n_per_pa'],
                           'train_mean_pa':p['mean_pa'],'train_sigma_pa':p['sigma_pa']} for p in result['selected_probes']],
                 'holdout_basis':result['holdout'],
                 'certificate_codes':[p['certificate_code'] for p in result['selected_probes']],
                 'certificates':[certificate_view(s,certs[p['probe_id']]) for p in result['selected_probes']],
                 'probe_ids':result['selected_probe_ids'],'signed_by':u.email,'signed_at':x.signed_at}
    audit(s,u,f'审签布置升力偏差 {l.code} 上限{x.limit_n}N 最大{result["max_abs_deviation_n"]}N')
    return x

def view(s,l):
    x=_layouts_check(s,l.id)
    runs=[]
    if x:
        for r in s.scalars(select(RunLiftCheck).where(RunLiftCheck.layout_check_id==x.id).order_by(RunLiftCheck.sequence)):
            runs.append(pack(r))
    return {'layout_id':l.id,'check':pack(x) if x else None,'run_checks':runs,'today':date.today().isoformat()}

def recheck_run(s,r,u):
    """开车前复核：最新证书 + 原模型，核算与留档共用同一组证书。"""
    l=s.get(Layout,r.layout_id)
    if not l or l.status!='signed':raise HTTPException(409,'布置尚未审签，不能复核升力偏差')
    if not _layouts_check(s,l.id):backfill(s);s.flush()
    x=_layout_check(s,l.id)
    if not x or not x.signed_by:raise HTTPException(409,'布置升力偏差尚未审签，不能复核开车')
    ids=selected(s,l.id)
    last=s.scalar(select(RunLiftCheck).where(RunLiftCheck.run_id==r.id).order_by(RunLiftCheck.sequence.desc()).with_for_update())
    row=RunLiftCheck(run_id=r.id,layout_check_id=x.id,sequence=(last.sequence+1) if last else 1,
                     status='failed',attempted_at=_now())
    s.add(row)
    def fail(msg,meta=None,recalc=None,cert_map=None):
        row.status='failed';row.result=recalc;row.error=msg
        row.evidence={'layout_id':l.id,'layout_code':l.code,'limit_n':x.limit_n,'checked_at':_now(),
                      'signed_basis':deepcopy(x.signature),'layout_check_id':x.id,
                      'prerequisites':meta or row.prerequisites,
                      'certificates':[certificate_view(s,c) for c in (cert_map or {}).values()],
                      'recomputed':deepcopy(recalc) if recalc else None,
                      'within_limit':recalc['within_limit'] if recalc else False,
                      'max_abs_deviation_n':recalc['max_abs_deviation_n'] if recalc else None}
        s.flush();s.commit();raise HTTPException(409,msg)
    if x.synthesized:fail('该布置为旧数据升级，升力偏差仅保留原签名、无原模型留出依据；须另签新布置方可开车')
    if not x.result or set(x.result['selected_probe_ids'])!=set(ids):fail('布置测点与升力偏差审签依据不一致')
    if x.result['source_checksum']!=source_checksum(s):fail('扫角来源已变化，布置升力偏差依据失效')
    if x.result['limit_n']!=x.limit_n or not x.pass_ready:fail('布置升力偏差当前依据未达标')
    meta,certs=resolve_certificates(s,ids)
    row.prerequisites=meta
    row.certificate_ids=list(meta['certificate_ids'])
    if meta['synthesized']:fail('最新证书复核未通过：'+'; '.join(meta['problems']),meta,cert_map=certs)
    try:recalc=compute(s,l,x.limit_n,certs)
    except HTTPException as e:fail('复核无法完成：'+str(e.detail),meta,cert_map=certs)
    row.result=recalc
    row.evidence={'layout_id':l.id,'layout_code':l.code,'limit_n':x.limit_n,'checked_at':_now(),
                  'signed_basis':deepcopy(x.signature),'layout_check_id':x.id,
                  'prerequisites':meta,'certificates':[certificate_view(s,c) for c in certs.values()],
                  'recomputed':deepcopy(recalc),'within_limit':recalc['within_limit'],
                  'max_abs_deviation_n':recalc['max_abs_deviation_n']}
    if not recalc['within_limit']:
        msg=f'最新证书复核后升力偏差最大{recalc["max_abs_deviation_n"]}N，超过上限{x.limit_n}N，退回重新核签布置'
        row.status='failed';row.error=msg;s.flush();s.commit();raise HTTPException(409,msg)
    row.status='ready';row.error=None;s.flush()
    return row.evidence,certs
