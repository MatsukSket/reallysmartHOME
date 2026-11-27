"""Хранение одного экземпляра решателя. Снимки JSON и наблюдения фиксируются одной транзакцией."""
import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import fields, is_dataclass
from datetime import datetime, time
from pathlib import Path
from .. import domain
from ..auth import User, Invite, DeviceSession, hash_code
from ..journal import Journal

def encode(obj):
    if isinstance(obj, datetime): return {"$datetime":obj.isoformat()}
    if isinstance(obj,time): return {"$time":obj.isoformat()}
    if is_dataclass(obj):
        return {f.name:getattr(obj,f.name) for f in fields(obj) if f.name!="wake"}
    raise TypeError(type(obj).__name__)

def decode(v):
    if "$datetime" in v: return datetime.fromisoformat(v["$datetime"])
    if "$time" in v: return time.fromisoformat(v["$time"])
    return v

def pack(v): return json.dumps(v,default=encode,ensure_ascii=False)
def unpack(v): return json.loads(v,object_hook=decode)

class Store:
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self.connect()) as db:
            version=db.execute("PRAGMA user_version").fetchone()[0]
            if version==0:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone():
                    raise ValueError('База содержит неизвестную схему. Задайте новый HOME_DB.')
                db.executescript("BEGIN;"+Path(__file__).with_name("migrations").joinpath("001.sql").read_text()+"COMMIT;")
            elif version!=1: raise ValueError("Неизвестная версия БД")
        self.cache={}
    def connect(self): return sqlite3.connect(self.path,timeout=10)
    def load(self,key):
        with closing(self.connect()) as db:
            row=db.execute("SELECT body FROM snapshots WHERE name=?",(key,)).fetchone()
            if row: return unpack(row[0])
    def save(self,key,value,observations=()):
        body=pack(value)
        if self.cache.get(key)==body and not observations: return
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?)",(key,body))
            db.executemany("INSERT INTO observations(event_id,body) VALUES (?,?)",
                           [(v.get("event_id"),pack(v)) for v in observations])
        self.cache[key]=body

class CheckpointLock:
    def __init__(self,lock,checkpoint):
        self.lock,self.checkpoint=lock,checkpoint
        self.depth=0
    def __enter__(self):
        self.lock.acquire()
        self.depth+=1
    def __exit__(self,*exc):
        try:
            self.depth-=1
            if self.depth==0: self.checkpoint()
        finally: self.lock.release()

class Audit(Journal):
    def __init__(self,store):
        super().__init__()
        self.store=store
        with closing(store.connect()) as db:
            self._records.extend(unpack(row[0]) for row in db.execute("SELECT body FROM audit ORDER BY seq"))
    def write(self,*args,**kwargs):
        row=super().write(*args,**kwargs)
        with closing(self.store.connect()) as db, db:
            db.execute("INSERT INTO audit(body) VALUES (?)",(pack(row),))
        return row

def attach(s,settings):
    db=Store(settings.get("HOME_DB",Path(__file__).resolve().parents[3]/"runtime"/"home.sqlite3"))
    s.database=db
    st=s.storage
    s.journal=Audit(db)
    old=db.load("home")
    simulation = hasattr(s.model, "dispatch")
    if old:
        initial_devices = st.devices()
        for key,cls in (("devices",domain.Device),("rooms",domain.Room)):
            setattr(st,"_"+key,{d["id"]:cls(**d) for d in old[key]})
        for device in initial_devices:
            saved = st.device(device.id)
            if saved and not saved.model and device.model and saved.type == device.type:
                saved.model = device.model
                st.save_device(saved)
            if simulation and device.id == "sensor_temperature_living" and not old.get("standalone_thermometer"):
                if st.room(device.room_id) is None:
                    device.connected = False
                    device.room_id = ""
                    device.name = "Датчик температуры"
                st.save_device(device)
            elif not device.connected and st.device(device.id) is None:
                st.save_device(device)
        st._targets={(v["room_id"],v["parameter"]):domain.Target(**v) for v in old["targets"]}
        s.locks={v["device_id"]:domain.Lock(**v) for v in old["locks"]}
        s.schedules=[domain.ScheduleMoment(**v) for v in old["schedules"]]
        s.pairs=[domain.SchedulePair(**v) for v in old["pairs"]]
        for p in old["pending"]:
            s.journal.write(p["impact_id"],"шлюз","перезапуск",subject=p["device_id"],status="не подтверждено",reason=p["model_id"],at=s.clock.now())
    with closing(db.connect()) as conn:
        st._history=[unpack(r[0]) for r in conn.execute("SELECT body FROM observations ORDER BY seq")]
    st._event_ids={r['event_id'] for r in st._history if r.get('event_id')}
    count=len(st._history)
    def save():
        nonlocal count
        db.save("home",dict(standalone_thermometer=simulation or bool(old and old.get("standalone_thermometer")),
                            devices=list(st._devices.values()),rooms=st.rooms(),targets=st.targets(),
                            locks=list(s.locks.values()),pending=list(s.pending.values()),schedules=s.schedules,pairs=s.pairs),
                st._history[count:])
        count=len(st._history)
    save()
    s._guard=CheckpointLock(s._guard,save)

def attach_auth(a,db):
    old=db.load("auth")
    if old:
        a._users={v["username"]:User(**v) for v in old["users"]}
        a._invites={v["code_hash"]:Invite(**v) for v in old["invites"]}
        for v in old["sessions"]:
            a._devices_by_refresh[v.pop("digest")]=v["id"]
            a._devices_by_id[v["id"]]=DeviceSession(**v,refresh_token="")
    def save():
        sessions=[{**{k:v for k,v in vars(a._devices_by_id[i]).items() if k!="refresh_token"},"digest":digest}
                  for digest,i in a._devices_by_refresh.items() if i in a._devices_by_id]
        db.save("auth",dict(users=list(a._users.values()),invites=list(a._invites.values()),sessions=sessions))
    a._lock=CheckpointLock(a._lock,save)
