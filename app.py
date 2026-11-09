"""Shift Roster & On-Call Manager. Run: uvicorn app:app --reload"""
import sqlite3, io, calendar, datetime as dt
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

DB = "roster.db"
app = FastAPI(title="Roster")
SHIFTS = {"S1": ("09:00", "18:15", "WFO"), "S2": ("15:15", "00:30", "WFO"), "S3": ("00:15", "09:30", "WFH")}
COLORS = {"S1": "BFE3C0", "S2": "9FD0F0", "S3": "F5B83D", "WO": "F0F03C", "WFO": "E8663D",
          "PL": "E33333", "UL": "AA0000", "GH": "7B2FBF", "CO": "CCCCCC"}
SEED = [  # id, name, phone, team, fixed-WFO (on-site 9-5 pattern as in sheet)
    ("EMP001", "Aarav Sharma", "+91-98001-11001", "Team A", 0), ("EMP002", "Priya Nair", "+91-98001-11002", "Team A", 0),
    ("EMP003", "Rohan Verma", "+91-98001-11003", "Team A", 1), ("EMP004", "Sneha Kapur", "+91-98001-11004", "Team A", 1),
    ("EMP005", "Vikram Singh", "+91-98001-11005", "Team B", 0), ("EMP006", "Ananya Ghosh", "+91-98001-11006", "Team B", 0),
    ("EMP007", "Karan Mehta", "+91-98001-11007", "Team B", 0), ("EMP008", "Divya Redy", "+91-98001-11008", "Team B", 0),
    ("EMP009", "Manish Gupta", "+91-98001-11009", "Team B", 0), ("EMP010", "Ritka Joshi", "+91-98001-11010", "Team C", 0),
    ("EMP011", "Surav Patel", "+91-98001-11011", "Team C", 0), ("EMP012", "Nisha Rao", "+91-98001-11012", "Team C", 0),
    ("EMP013", "Amit Chodhary", "+91-98001-11013", "Team C", 0), ("EMP014", "Puja Desai", "+91-98001-11014", "Team C", 0),
    ("EMP015", "Rahol Kumar", "+91-98001-11015", "Team D", 0), ("EMP016", "Tanvi Shah", "+91-98001-11016", "Team D", 0),
    ("EMP017", "Gaurav Mishra", "+91-98001-11017", "Team D", 1), ("EMP018", "Isha Banerge", "+91-98001-11018", "Team D", 0),
    ("EMP019", "Neha Sinha", "+91-98001-11019", "Team D", 0), ("EMP020", "Yash Thakur", "+91-98001-11020", "Team D", 0)]


def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c


@app.on_event("startup")
def init():
    c = db()
    c.executescript("""create table if not exists emp(id text primary key,name,phone,team,fixed int);
    create table if not exists roster(emp text,d text,code text,primary key(emp,d));
    create table if not exists oncall(team,d,emp,primary_emp,primary key(team,d));
    create table if not exists leaves(emp,d,code,primary key(emp,d));
    create table if not exists audit(ts,user,emp,d,old,new,reason);""")
    if not c.execute("select 1 from emp").fetchone():
        c.executemany("insert into emp values(?,?,?,?,?)", SEED)
    c.commit()


def log(c, user, emp, d, old, new, reason=""):
    c.execute("insert into audit values(?,?,?,?,?,?,?)", (dt.datetime.now().isoformat(timespec="seconds"), user, emp, d, old, new, reason))


# ---------------- Roster engine ----------------
OC_TEAMS = {"Team B", "Team C", "Team D"}      # teams that run on-call (as in the sheet)
ABSENT = ("PL", "UL", "CO")


class Gen(BaseModel):
    ym: str
    holidays: list[str] = []
    wfo: list[str] | None = None     # EmpIDs who are WFO -> auto-assigned WFO on every weekday


def pick_backup(cands, bk, R, d):
    return min(cands, key=lambda i: (R[(i, d)] == "S3", bk.get(i, 0), i)) if cands else ""


@app.post("/api/generate")
def generate(b: Gen):
    y, m = map(int, b.ym.split("-"))
    days = [dt.date(y, m, i) for i in range(1, calendar.monthrange(y, m)[1] + 1)]
    c = db()
    if b.wfo is not None:
        c.execute("update emp set fixed=0")
        c.executemany("update emp set fixed=1 where id=?", [(i.strip().upper(),) for i in b.wfo if i.strip()])
    emps = [dict(r) for r in c.execute("select * from emp order by id")]
    lv = {(r["emp"], r["d"]): r["code"] for r in c.execute("select * from leaves")}
    teams = {}
    for e in emps: teams.setdefault(e["team"], []).append(e)
    R = {}
    for ms in teams.values():                         # weekly S1->S2->S3 rotation, offset per person
        rot = [e for e in ms if not e["fixed"]]
        for d in days:
            wk = d.isocalendar()[1]
            for e in ms:
                if str(d) in b.holidays: k = "GH"
                elif d.weekday() >= 5: k = "WO"
                elif (e["id"], str(d)) in lv: k = lv[(e["id"], str(d))]
                elif e["fixed"]: k = "WFO"
                else: k = ["S1", "S2", "S3"][(rot.index(e) + wk) % 3]
                R[(e["id"], d)] = k
    oc, bk = {}, {}
    for t, ms in teams.items():                       # ONE person per ISO week (Mon-Sun) per team
        el = [e["id"] for e in ms if not e["fixed"]]
        if t not in OC_TEAMS or not el: continue
        for d in days:
            iy, wk, _ = d.isocalendar()
            prim = el[(iy * 53 + wk) % len(el)]
            who = prim
            if R[(prim, d)] in ABSENT:                # primary absent that day -> same-team backup for that day only
                who = pick_backup([i for i in el if i != prim and R[(i, d)] not in ABSENT], bk, R, d)
                if who: bk[who] = bk.get(who, 0) + 1
            oc[(t, d)] = (who, prim)
    for tb in ("roster", "oncall"): c.execute(f"delete from {tb} where d like ?", (b.ym + "%",))
    c.executemany("insert into roster values(?,?,?)", [(e, str(d), k) for (e, d), k in R.items()])
    c.executemany("insert into oncall values(?,?,?,?)", [(t, str(d), w, p) for (t, d), (w, p) in oc.items()])
    log(c, "system", "*", b.ym, "", "generated"); c.commit()
    return {"days": len(days), "backups_used": bk}


@app.get("/api/roster")
def roster(ym: str, team: str = "", emp: str = ""):
    c = db()
    emps = c.execute("select * from emp where (team=? or ?='') and (id=? or ?='') order by id", (team, team, emp, emp)).fetchall()
    names = {r["id"]: r["name"] for r in c.execute("select id,name from emp")}
    cells = {(r["emp"], r["d"]): r["code"] for r in c.execute("select * from roster where d like ?", (ym + "%",))}
    oc = {}
    for r in c.execute("select * from oncall where d like ?", (ym + "%",)):
        oc.setdefault(r["team"], {})[r["d"]] = {"emp": r["emp"], "name": names.get(r["emp"], "—"), "backup": r["emp"] != r["primary_emp"]}
    y, m = map(int, ym.split("-"))
    dates = [str(dt.date(y, m, i)) for i in range(1, calendar.monthrange(y, m)[1] + 1)]
    return {"dates": dates, "oc": oc, "teams": sorted({r["team"] for r in c.execute("select team from emp")}),
            "wfo": [r["id"] for r in c.execute("select id from emp where fixed=1")],
            "rows": [{"id": e["id"], "name": e["name"], "team": e["team"], "phone": e["phone"],
                      "cells": [cells.get((e["id"], d), "") for d in dates]} for e in emps]}


# ---------------- Leave + dynamic re-allocation ----------------
class Leave(BaseModel):
    emp: str; d: str; code: str; user: str = "manager"


@app.post("/api/leave")
def leave(b: Leave):
    if b.code not in ABSENT: raise HTTPException(400, "code must be PL/UL/CO")
    c = db()
    me = c.execute("select * from emp where id=?", (b.emp,)).fetchone()
    if not me: raise HTTPException(404, "unknown employee")
    old = (c.execute("select code from roster where emp=? and d=?", (b.emp, b.d)).fetchone() or [""])[0]
    c.execute("insert or replace into leaves values(?,?,?)", (b.emp, b.d, b.code))
    c.execute("insert or replace into roster values(?,?,?)", (b.emp, b.d, b.code)); log(c, b.user, b.emp, b.d, old, b.code, "leave")
    code_of = lambda i: (c.execute("select code from roster where emp=? and d=?", (i, b.d)).fetchone() or [""])[0]
    msgs, sug = [], []
    # 1) On-call: the weekly owner is absent today -> auto-assign same-team backup for this day only
    o = c.execute("select * from oncall where team=? and d=? and emp=?", (me["team"], b.d, b.emp)).fetchone()
    if o:
        el = [r["id"] for r in c.execute("select id from emp where team=? and fixed=0 and id!=?", (me["team"], b.emp)) if code_of(r["id"]) not in ABSENT]
        bk = {r[0]: r[1] for r in c.execute("select emp,count(*) from oncall where team=? and emp!=primary_emp group by emp", (me["team"],))}
        R = {(i, b.d): code_of(i) for i in el}
        w = pick_backup(el, bk, R, b.d)
        c.execute("update oncall set emp=? where team=? and d=?", (w, me["team"], b.d))
        msgs.append(f"On-call for {me['team']} on {b.d}: backup {w or 'NONE AVAILABLE'} assigned for this day only.")
    # 2) Shift gap: only a real gap if nobody else in the team still works that shift
    gap = False
    if old in SHIFTS:
        team = c.execute("select id from emp where team=?", (me["team"],)).fetchall()
        left = sum(1 for r in team if r["id"] != b.emp and code_of(r["id"]) == old)
        if left:
            msgs.append(f"{old} is still covered by {left} other member(s) of {me['team']} - no replacement needed.")
        else:
            gap = True
            yd = str(dt.date.fromisoformat(b.d) - dt.timedelta(days=1)); td = str(dt.date.fromisoformat(b.d) + dt.timedelta(days=1))
            cnt = {}
            for r in c.execute("select e.team,r.code,count(*) n from roster r join emp e on e.id=r.emp where r.d=? group by e.team,r.code", (b.d,)): cnt[(r[0], r[1])] = r[2]
            for e in c.execute("select * from emp where fixed=0 and id!=? order by id", (b.emp,)):
                k = code_of(e["id"]); pk = (c.execute("select code from roster where emp=? and d=?", (e["id"], yd)).fetchone() or [""])[0]
                nk = (c.execute("select code from roster where emp=? and d=?", (e["id"], td)).fetchone() or [""])[0]
                if (pk in ("S2", "S3") and old == "S1") or (old == "S3" and nk == "S1"): continue   # rest rule
                if k in ("WO", "CO"): typ, note = "off-duty", f"Covers {old}; owed a comp-off"
                elif k in SHIFTS and k != old and cnt.get((e["team"], k), 0) >= 2:
                    typ, note = "move", f"Moves {k}->{old}; {k} stays covered by {cnt[(e['team'], k)] - 1}"
                else: continue
                sug.append({"emp": e["id"], "name": e["name"], "team": e["team"], "current": k, "type": typ, "note": note,
                            "_r": (typ != "off-duty", e["team"] != me["team"], e["id"])})
            sug.sort(key=lambda s: s["_r"])
            for s in sug: s.pop("_r")
            msgs.append(f"{old} on {b.d} has NO cover in {me['team']}." + ("" if sug else " No safe replacement (every option would open a new gap) - escalate / cross-team help."))
    c.commit()
    return {"gap": gap or bool(o), "needs": [old] if gap else [], "suggestions": sug[:6], "messages": msgs}


# ---------------- Admin override + audit ----------------
class Override(BaseModel):
    emp: str; d: str; code: str; user: str = "manager"; reason: str = ""


@app.post("/api/override")
def override(b: Override):
    c = db()
    if b.code == "OC":
        t = c.execute("select team from emp where id=?", (b.emp,)).fetchone()[0]
        old = (c.execute("select emp from oncall where team=? and d=?", (t, b.d)).fetchone() or [""])[0]
        c.execute("update oncall set emp=? where team=? and d=?", (b.emp, t, b.d)); log(c, b.user, b.emp, b.d, "OC:" + old, "OC", b.reason)
    else:
        if b.code not in COLORS: raise HTTPException(400, "unknown code")
        old = (c.execute("select code from roster where emp=? and d=?", (b.emp, b.d)).fetchone() or [""])[0]
        c.execute("insert or replace into roster values(?,?,?)", (b.emp, b.d, b.code)); log(c, b.user, b.emp, b.d, old, b.code, b.reason)
        if b.code in ABSENT: c.execute("insert or replace into leaves values(?,?,?)", (b.emp, b.d, b.code))
        else: c.execute("delete from leaves where emp=? and d=?", (b.emp, b.d))
    c.commit(); return {"ok": True}


@app.get("/api/audit")
def audit(): return [dict(r) for r in db().execute("select * from audit order by ts desc limit 50")]


# ---------------- Export + UI ----------------
@app.get("/api/export.xlsx")
def export(ym: str, team: str = ""):
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill, Font, Alignment
    r = roster(ym, team); wb = Workbook(); ws = wb.active; ws.title = ym
    fill = lambda h: PatternFill("solid", fgColor=h)
    ws.append(["EmpID", "Name", "Team", "Contact"] + [dt.date.fromisoformat(d).strftime("%a %d/%m") for d in r["dates"]])
    for x in ws[1]: x.font = Font(bold=True, color="FFFFFF"); x.fill = fill("1F3864"); x.alignment = Alignment(horizontal="center")
    for t in sorted({x["team"] for x in r["rows"]}):
        ws.append([t]); ws.merge_cells(start_row=ws.max_row, start_column=1, end_row=ws.max_row, end_column=4 + len(r["dates"]))
        ws.cell(ws.max_row, 1).font = Font(bold=True); ws.cell(ws.max_row, 1).fill = fill("D9E1F2")
        if t in r["oc"]:
            ws.append(["", "On-Call", t, ""] + [(r["oc"][t].get(d, {}).get("name", "") + (" *" if r["oc"][t].get(d, {}).get("backup") else "")) for d in r["dates"]])
            for x in ws[ws.max_row][:]: x.fill = fill("E4D4F4"); x.font = Font(bold=True)
        for row in [x for x in r["rows"] if x["team"] == t]:
            ws.append([row["id"], row["name"], t, row["phone"]] + row["cells"])
            for x in ws[ws.max_row][4:]: x.fill = fill(COLORS.get(x.value, "FFFFFF")); x.alignment = Alignment(horizontal="center")
    for col, w in zip("ABCD", (10, 18, 9, 17)): ws.column_dimensions[col].width = w
    ws.freeze_panes = "E2"
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f"attachment; filename=roster_{ym}.xlsx"})


@app.get("/")
def home(): return FileResponse("index.html")