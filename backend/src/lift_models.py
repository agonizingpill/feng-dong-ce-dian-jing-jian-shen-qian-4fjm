from sqlalchemy import String,Integer,Float,Boolean,JSON,ForeignKey,CheckConstraint,UniqueConstraint
from sqlalchemy.orm import Mapped,mapped_column
from .db import Base
class LayoutLiftCheck(Base):
    __tablename__='layout_lift_checks'
    __table_args__=(CheckConstraint('limit_n IS NULL OR limit_n > 0'),CheckConstraint('sequence >= 0'))
    id:Mapped[int]=mapped_column(primary_key=True)
    layout_id:Mapped[int]=mapped_column(ForeignKey('layouts.id'),unique=True)
    limit_n:Mapped[float|None]=mapped_column(Float,nullable=True)
    sequence:Mapped[int]=mapped_column(Integer,default=1)
    pass_ready:Mapped[bool]=mapped_column(Boolean,default=False)
    status:Mapped[str]=mapped_column(String(20),default='calculating')
    result:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    error:Mapped[str|None]=mapped_column(String(500),nullable=True)
    synthesized:Mapped[bool]=mapped_column(Boolean,default=False)
    prerequisites:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    signed_by:Mapped[str|None]=mapped_column(String(100),nullable=True)
    signed_at:Mapped[str|None]=mapped_column(String(40),nullable=True)
    signature:Mapped[dict|None]=mapped_column(JSON,nullable=True)
class RunLiftCheck(Base):
    __tablename__='run_lift_checks'
    __table_args__=(CheckConstraint('sequence > 0'),CheckConstraint("status in ('ready','failed')"),
                    UniqueConstraint('run_id','sequence'))
    id:Mapped[int]=mapped_column(primary_key=True)
    run_id:Mapped[int]=mapped_column(ForeignKey('tunnel_runs.id'))
    layout_check_id:Mapped[int]=mapped_column(ForeignKey('layout_lift_checks.id'))
    sequence:Mapped[int]=mapped_column(Integer)
    status:Mapped[str]=mapped_column(String(20))
    result:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    error:Mapped[str|None]=mapped_column(String(500),nullable=True)
    prerequisites:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    evidence:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    certificate_ids:Mapped[list|None]=mapped_column(JSON,nullable=True)
    attempted_at:Mapped[str|None]=mapped_column(String(40),nullable=True)
