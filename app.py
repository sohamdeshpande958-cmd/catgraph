"""CatGraph — local store for a category DAG + entries with multiple parent categories.

Storage: a single SQLite file (DB_PATH, default /data/catgraph.db).
API:     FastAPI, docs at /docs. GUI served at /.
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DB_PATH = os.environ.get("DB_PATH", "/data/catgraph.db")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS category (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS category_edge (
    parent_id INTEGER NOT NULL REFERENCES category(id) ON DELETE CASCADE,
    child_id  INTEGER NOT NULL REFERENCES category(id) ON DELETE CASCADE,
    PRIMARY KEY (parent_id, child_id),
    CHECK (parent_id <> child_id)
);
CREATE TABLE IF NOT EXISTS entry (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ext_id      TEXT UNIQUE,              -- your own ID (optional)
    description TEXT NOT NULL DEFAULT '',
    data        TEXT                      -- optional JSON
);
CREATE TABLE IF NOT EXISTS entry_category (
    entry_id    INTEGER NOT NULL REFERENCES entry(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES category(id) ON DELETE CASCADE,
    PRIMARY KEY (entry_id, category_id)
);
CREATE INDEX IF NOT EXISTS idx_edge_child ON category_edge(child_id);
CREATE INDEX IF NOT EXISTS idx_ec_cat ON entry_category(category_id);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    with db() as c:
        c.executescript(SCHEMA)
        c.execute("PRAGMA journal_mode = WAL")


init_db()
app = FastAPI(title="CatGraph", version="1.1")


# ---------- models ----------
class CategoryIn(BaseModel):
    name: str
    description: str = ""
    parent_ids: list[int] = []


class CategoryPatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class EdgeIn(BaseModel):
    parent_id: int
    child_id: int


class EntryIn(BaseModel):
    ext_id: Optional[str] = None
    description: str = ""
    data: Optional[Any] = None
    category_ids: list[int] = []


class EntryPatch(BaseModel):
    ext_id: Optional[str] = None
    description: Optional[str] = None
    data: Optional[Any] = None
    category_ids: Optional[list[int]] = None


# ---------- helpers ----------
def descendants(c, cat_id: int) -> list[int]:
    rows = c.execute(
        """WITH RECURSIVE sub(id) AS (
               SELECT ? UNION
               SELECT e.child_id FROM category_edge e JOIN sub ON e.parent_id = sub.id)
           SELECT id FROM sub""",
        (cat_id,),
    ).fetchall()
    return [r["id"] for r in rows]


def ancestors(c, cat_id: int) -> list[int]:
    rows = c.execute(
        """WITH RECURSIVE up(id) AS (
               SELECT ? UNION
               SELECT e.parent_id FROM category_edge e JOIN up ON e.child_id = up.id)
           SELECT id FROM up""",
        (cat_id,),
    ).fetchall()
    return [r["id"] for r in rows]


def require(c, table: str, id_: int):
    if not c.execute(f"SELECT 1 FROM {table} WHERE id=?", (id_,)).fetchone():
        raise HTTPException(404, f"{table} {id_} not found")


def add_edge(c, parent_id: int, child_id: int):
    if parent_id == child_id:
        raise HTTPException(400, "A category cannot be its own parent")
    require(c, "category", parent_id)
    require(c, "category", child_id)
    # adding parent->child creates a cycle iff parent is already a descendant of child
    if parent_id in descendants(c, child_id):
        raise HTTPException(400, "That relation would create a cycle")
    c.execute("INSERT OR IGNORE INTO category_edge(parent_id, child_id) VALUES (?,?)", (parent_id, child_id))


def dump_data(data):
    # `data` arrives already parsed from the request JSON, so any value is valid JSON
    return None if data is None else json.dumps(data)


def entry_row(c, r) -> dict:
    cats = c.execute(
        "SELECT c.id, c.name FROM entry_category ec JOIN category c ON c.id = ec.category_id "
        "WHERE ec.entry_id=? ORDER BY c.name",
        (r["id"],),
    ).fetchall()
    return {
        "id": r["id"],
        "ext_id": r["ext_id"],
        "description": r["description"],
        "data": json.loads(r["data"]) if r["data"] else None,
        "categories": [dict(x) for x in cats],
    }


def set_entry_cats(c, entry_id: int, cat_ids: list[int]):
    for cid in cat_ids:
        require(c, "category", cid)
    c.execute("DELETE FROM entry_category WHERE entry_id=?", (entry_id,))
    c.executemany(
        "INSERT OR IGNORE INTO entry_category(entry_id, category_id) VALUES (?,?)",
        [(entry_id, cid) for cid in set(cat_ids)],
    )


# ---------- categories ----------
@app.get("/api/categories")
def list_categories():
    """All categories, each with parent_ids, child_ids, ancestor_ids (all recursive parents)
    and direct_entries. The graph is walked in memory from one edge query."""
    with db() as c:
        cats = [dict(r) for r in c.execute("SELECT id, name, description FROM category ORDER BY name")]
        edges = [dict(r) for r in c.execute("SELECT parent_id, child_id FROM category_edge")]
        counts = {
            r["category_id"]: r["n"]
            for r in c.execute("SELECT category_id, COUNT(*) n FROM entry_category GROUP BY category_id")
        }

    parents_of: dict[int, list[int]] = {}
    children_of: dict[int, list[int]] = {}
    for e in edges:
        parents_of.setdefault(e["child_id"], []).append(e["parent_id"])
        children_of.setdefault(e["parent_id"], []).append(e["child_id"])

    def all_ancestors(cid: int) -> list[int]:
        seen, stack = set(), list(parents_of.get(cid, []))
        while stack:
            p = stack.pop()
            if p not in seen and p != cid:
                seen.add(p)
                stack.extend(parents_of.get(p, []))
        return sorted(seen)

    for x in cats:
        x["parent_ids"] = parents_of.get(x["id"], [])
        x["child_ids"] = children_of.get(x["id"], [])
        x["ancestor_ids"] = all_ancestors(x["id"])
        x["direct_entries"] = counts.get(x["id"], 0)
    return {"categories": cats, "edges": edges}


@app.post("/api/categories", status_code=201)
def create_category(body: CategoryIn):
    with db() as c:
        try:
            cur = c.execute("INSERT INTO category(name, description) VALUES (?,?)", (body.name.strip(), body.description))
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"Category '{body.name}' already exists")
        cid = cur.lastrowid
        for pid in body.parent_ids:
            add_edge(c, pid, cid)
    return {"id": cid}


@app.patch("/api/categories/{cid}")
def update_category(cid: int, body: CategoryPatch):
    with db() as c:
        require(c, "category", cid)
        try:
            if body.name is not None:
                c.execute("UPDATE category SET name=? WHERE id=?", (body.name.strip(), cid))
            if body.description is not None:
                c.execute("UPDATE category SET description=? WHERE id=?", (body.description, cid))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Name already in use")
    return {"ok": True}


@app.delete("/api/categories/{cid}")
def delete_category(cid: int):
    with db() as c:
        require(c, "category", cid)
        c.execute("DELETE FROM category WHERE id=?", (cid,))
    return {"ok": True}


@app.get("/api/categories/{cid}/descendants")
def get_descendants(cid: int):
    with db() as c:
        require(c, "category", cid)
        return {"ids": descendants(c, cid)}


@app.get("/api/categories/{cid}/ancestors")
def get_ancestors(cid: int):
    with db() as c:
        require(c, "category", cid)
        return {"ids": ancestors(c, cid)}


# ---------- edges ----------
@app.post("/api/edges", status_code=201)
def create_edge(body: EdgeIn):
    with db() as c:
        add_edge(c, body.parent_id, body.child_id)
    return {"ok": True}


@app.delete("/api/edges")
def delete_edge(parent_id: int, child_id: int):
    with db() as c:
        c.execute("DELETE FROM category_edge WHERE parent_id=? AND child_id=?", (parent_id, child_id))
    return {"ok": True}


# ---------- entries ----------
@app.get("/api/entries")
def list_entries(
    category_id: Optional[int] = None,
    recursive: bool = True,
    q: Optional[str] = None,
    limit: int = Query(200, le=5000),
    offset: int = 0,
):
    with db() as c:
        where, params = [], []
        if category_id is not None:
            require(c, "category", category_id)
            ids = descendants(c, category_id) if recursive else [category_id]
            where.append(
                f"e.id IN (SELECT entry_id FROM entry_category WHERE category_id IN ({','.join('?' * len(ids))}))"
            )
            params += ids
        if q:
            where.append("(e.description LIKE ? OR e.ext_id LIKE ? OR e.data LIKE ?)")
            params += [f"%{q}%"] * 3
        sql_where = ("WHERE " + " AND ".join(where)) if where else ""
        total = c.execute(f"SELECT COUNT(*) FROM entry e {sql_where}", params).fetchone()[0]
        rows = c.execute(
            f"SELECT * FROM entry e {sql_where} ORDER BY e.id DESC LIMIT ? OFFSET ?", params + [limit, offset]
        ).fetchall()
        return {"total": total, "entries": [entry_row(c, r) for r in rows]}


@app.get("/api/entries/{eid}")
def get_entry(eid: int):
    with db() as c:
        r = c.execute("SELECT * FROM entry WHERE id=?", (eid,)).fetchone()
        if not r:
            raise HTTPException(404, "entry not found")
        return entry_row(c, r)


@app.post("/api/entries", status_code=201)
def create_entry(body: EntryIn):
    with db() as c:
        try:
            cur = c.execute(
                "INSERT INTO entry(ext_id, description, data) VALUES (?,?,?)",
                (body.ext_id or None, body.description, dump_data(body.data)),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"Entry ID '{body.ext_id}' already exists")
        set_entry_cats(c, cur.lastrowid, body.category_ids)
        return {"id": cur.lastrowid}


@app.patch("/api/entries/{eid}")
def update_entry(eid: int, body: EntryPatch):
    with db() as c:
        require(c, "entry", eid)
        fields = body.model_dump(exclude_unset=True)
        try:
            if "ext_id" in fields:
                c.execute("UPDATE entry SET ext_id=? WHERE id=?", (body.ext_id or None, eid))
            if "description" in fields:
                c.execute("UPDATE entry SET description=? WHERE id=?", (body.description, eid))
            if "data" in fields:
                c.execute("UPDATE entry SET data=? WHERE id=?", (dump_data(body.data), eid))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Entry ID already in use")
        if body.category_ids is not None:
            set_entry_cats(c, eid, body.category_ids)
    return {"ok": True}


@app.delete("/api/entries/{eid}")
def delete_entry(eid: int):
    with db() as c:
        c.execute("DELETE FROM entry WHERE id=?", (eid,))
    return {"ok": True}


# ---------- export / import ----------
def _safe_name(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in s)[:60] or "category"


@app.get("/api/export")
def export_all(
    category_id: Optional[int] = None,
    recursive: bool = True,
    format: str = Query("json", pattern="^(json|csv)$"),
):
    """Export everything, or just one category (and, if recursive, its whole sub-graph).

    json -> categories, edges, entries and links (re-importable via /api/import)
    csv  -> one row per entry: id, ext_id, description, categories, data
    """
    with db() as c:
        if category_id is None:
            cat_ids, fname = None, "catgraph-export"
        else:
            require(c, "category", category_id)
            cat_ids = descendants(c, category_id) if recursive else [category_id]
            name = c.execute("SELECT name FROM category WHERE id=?", (category_id,)).fetchone()["name"]
            fname = f"catgraph-{_safe_name(name)}"

        def where(col):
            if cat_ids is None:
                return "", []
            return f" WHERE {col} IN ({','.join('?' * len(cat_ids))})", list(cat_ids)

        w, p = where("category_id")
        entry_ids = [r[0] for r in c.execute(f"SELECT DISTINCT entry_id FROM entry_category{w}", p)] \
            if cat_ids is not None else None
        if entry_ids is None:
            entries = c.execute("SELECT * FROM entry ORDER BY id").fetchall()
        else:
            entries = c.execute(
                f"SELECT * FROM entry WHERE id IN ({','.join('?' * len(entry_ids))}) ORDER BY id", entry_ids
            ).fetchall() if entry_ids else []

        if format == "csv":
            import csv, io
            buf = io.StringIO()
            wr = csv.writer(buf)
            wr.writerow(["id", "ext_id", "description", "categories", "data"])
            for r in entries:
                names = [x[0] for x in c.execute(
                    "SELECT c.name FROM entry_category ec JOIN category c ON c.id=ec.category_id "
                    "WHERE ec.entry_id=? ORDER BY c.name", (r["id"],))]
                wr.writerow([r["id"], r["ext_id"] or "", r["description"], "; ".join(names), r["data"] or ""])
            from starlette.responses import Response
            return Response(buf.getvalue(), media_type="text/csv",
                            headers={"Content-Disposition": f"attachment; filename={fname}.csv"})

        wc, pc = where("id")
        we, pe = ("", []) if cat_ids is None else (
            f" WHERE parent_id IN ({','.join('?' * len(cat_ids))}) AND child_id IN ({','.join('?' * len(cat_ids))})",
            list(cat_ids) * 2)
        ids_e = [r["id"] for r in entries]
        data = {
            "scope": {"category_id": category_id, "recursive": recursive},
            "categories": [dict(r) for r in c.execute(f"SELECT * FROM category{wc}", pc)],
            "category_edges": [dict(r) for r in c.execute(f"SELECT * FROM category_edge{we}", pe)],
            "entries": [{**dict(r), "data": json.loads(r["data"]) if r["data"] else None} for r in entries],
            # links only to categories inside the exported set, so the file re-imports cleanly
            "entry_categories": [dict(r) for r in c.execute("SELECT * FROM entry_category")
                                 if r["entry_id"] in set(ids_e)
                                 and (cat_ids is None or r["category_id"] in set(cat_ids))],
        }
    return JSONResponse(data, headers={"Content-Disposition": f"attachment; filename={fname}.json"})


@app.post("/api/import")
def import_all(payload: dict, replace: bool = False):
    """Restore an export. replace=true wipes the DB first; otherwise rows are merged by id."""
    with db() as c:
        if replace:
            for t in ("entry_category", "category_edge", "entry", "category"):
                c.execute(f"DELETE FROM {t}")
        for r in payload.get("categories", []):
            c.execute("INSERT OR REPLACE INTO category(id,name,description) VALUES (?,?,?)",
                      (r["id"], r["name"], r.get("description", "")))
        for r in payload.get("category_edges", []):
            c.execute("INSERT OR IGNORE INTO category_edge VALUES (?,?)", (r["parent_id"], r["child_id"]))
        for r in payload.get("entries", []):
            c.execute("INSERT OR REPLACE INTO entry(id,ext_id,description,data) VALUES (?,?,?,?)",
                      (r["id"], r.get("ext_id"), r.get("description", ""), dump_data(r.get("data"))))
        for r in payload.get("entry_categories", []):
            c.execute("INSERT OR IGNORE INTO entry_category VALUES (?,?)", (r["entry_id"], r["category_id"]))
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": True, "db": DB_PATH}


# ---------- GUI ----------
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
