import os,sys,pathlib
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
DB=pathlib.Path(__file__).resolve().parent/'test.sqlite3'
if DB.exists():DB.unlink()
os.environ['DATABASE_URL']='sqlite+pysqlite:///'+str(DB)
os.environ['REDIS_URL']='redis://127.0.0.1:6379/0'
os.environ['JWT_SECRET']='test-secret'
os.environ['SEED_DEMO']='false'
import pytest
from fastapi.testclient import TestClient
from src.db import Base,engine,Session
from src.models import Layout
from src.calibration_models import CalibrationCertificate
from scripts.seed_demo import seed
from src.main import app
from src import lift_service

@pytest.fixture(scope='session')
def client():
    from src import main as main_module
    main_module.r.rpush=lambda *a,**k: 1
    main_module.r.ping=lambda: True
    Base.metadata.create_all(engine);seed()
    with TestClient(app) as c:
        with Session() as s:lift_service.backfill(s);s.commit()
        yield c

def token(c,email='admin@yiheng.demo'):
    r=c.post('/auth/login',json={'email':email,'password':'Tunnel2026!'});assert r.status_code==200,r.text
    return {'Authorization':'Bearer '+r.json()['token']}

def cert(c,probe_code,zero=0.05,span=0.2,code=None,issue=True):
    h=token(c);p=next(x for x in c.get('/probes',headers=h).json() if x['code']==probe_code)
    code=code or f'CAL-T-{probe_code}-{zero}-{span}'
    r=c.post('/calibration-certificates',headers=h,json={'probe_id':p['id'],'code':code,'zero_error':zero,
        'span_error':span,'valid_until':'2027-12-31'})
    assert r.status_code==200,r.text
    if issue:
        r=c.post(f"/calibration-certificates/{r.json()['id']}/issue",headers=h);assert r.status_code==200,r.text
    return r.json()

def probe_ids(c):
    return {x['code']:x['id'] for x in c.get('/probes',headers=token(c)).json()}

@pytest.fixture(scope='session',autouse=True)
def base_certs(client):
    for code in ('P01','P05','P09'):
        cert(client,code,zero=0.05,span=0.2,code=f'CAL-BASE-{code}')
