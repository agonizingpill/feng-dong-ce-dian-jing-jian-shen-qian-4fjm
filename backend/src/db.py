import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
URL=os.environ['DATABASE_URL']
engine = create_engine(URL, pool_pre_ping=True, **({'isolation_level':'READ COMMITTED'} if not URL.startswith('sqlite') else {}))
Session = sessionmaker(engine, expire_on_commit=False)
class Base(DeclarativeBase): pass
def db_session():
    with Session() as s:
        yield s
