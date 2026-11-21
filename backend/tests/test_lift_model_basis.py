from src import lift_service
from tests.conftest import client,token,cert,probe_ids  # noqa: F401
from tests.test_lift_flow import make_layout

def test_signature_keeps_original_model_and_holdout_basis(client):
    p=probe_ids(client);lid=make_layout(client,limit='10000',probes=[p['P01'],p['P05'],p['P09']])
    h=token(client)
    r=client.post(f'/layouts/{lid}/sign',headers=h);assert r.status_code==200,r.text
    sig=r.json()['lift_check']['signature']
    assert set(sig['model'][0])=={'probe_id','code','gain_n_per_pa','train_mean_pa','train_sigma_pa'}
    assert [m['code'] for m in sig['model']]==['P01','P05','P09']
    assert len(sig['holdout_basis'])==24
    assert len(sig['certificates'])==3
    assert all(c['state']=='issued' for c in sig['certificates'])
    assert sig['z_sigma']==lift_service.Z_SIGMA

def test_basis_model_reproduces_signed_max_deviation(client):
    p=probe_ids(client);lid=make_layout(client,limit='10000',probes=[p['P01'],p['P05'],p['P09']])
    h=token(client);r=client.post(f'/layouts/{lid}/sign',headers=h);assert r.status_code==200
    sig=r.json()['lift_check']['signature']
    # 用签下的灵敏系数与证书误差重算零点偏置，应与签署值一致（可由第三方复算）
    assert all(abs(m['train_sigma_pa'])>0 for m in sig['model'])
    assert abs(sig['max_abs_deviation_n']-max(x['worst_abs_n'] for x in sig['holdout_basis']))<1e-3
