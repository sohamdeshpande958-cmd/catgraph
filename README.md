# CatGraph

A local store for **categories that form a graph (DAG, multiple parents)** and **entries that can belong to multiple categories**. SQLite storage, FastAPI backend, browser GUI. It runs fully offline.

## Build, push, pull

```bash
# 1. Build (inside this folder)
docker build -t svind/catgraph:latest .

# 2. Push
docker login
docker push svind/catgraph:latest

# 3. On any machine: pull and run
docker pull svind/catgraph:latest
docker run -d --name catgraph \
  -p 127.0.0.1:8008:8000 \
  -v ~/catgraph-data:/data \
  --restart unless-stopped \
  svind/catgraph:latest
```

Open **http://localhost:8008** for the GUI. The API docs are at **http://localhost:8008/docs**.

You can also use compose: `docker compose up -d --build`.

> **Your data lives in the mounted folder** (`~/catgraph-data/catgraph.db`), not in the image.
> Without `-v`, data disappears when the container is removed. Back up by copying that file,
> or use **Export JSON** in the GUI.

## Using the GUI

- **Left panel:** the category graph. A category with several parents shows up under *each* parent, and a `×N` badge marks multi-parent categories. Click a category to filter entries. Double-click it to edit.
- **+ Category:** set a name, plus any number of parents and children. Cycles are rejected.
- **+ Entry:** set an optional ID of your own, a description, any number of categories, and optional JSON.
- **include sub-categories:** when checked, selecting a category also shows entries in all of its descendants.
- **Search** matches the description, your ID, and the JSON contents.
- **Export CSV / Export JSON** (above the table): exports exactly what you're viewing — the selected category, plus its sub-categories if that box is ticked. CSV has one row per entry, with all of its categories. JSON includes the category sub-graph and can be re-imported.
- **Full backup / Import** (header): the whole database. On import you choose between replace and merge.
- API: `GET /api/export?category_id=5&recursive=true&format=csv` (or `format=json`; omit `category_id` for everything).

## Schema

| table | columns |
|---|---|
| `category` | id, name (unique), description |
| `category_edge` | parent_id, child_id (DAG, cycles blocked) |
| `entry` | id, ext_id (your ID, unique, optional), description, data (JSON text) |
| `entry_category` | entry_id, category_id |

Deleting a category removes only its links; its entries and child categories stay.

Because it's plain SQLite, you can also open the `.db` with `sqlite3` or DB Browser for SQLite, or script against the REST API, e.g.:

```bash
curl -X POST localhost:8000/api/categories -H 'Content-Type: application/json' -d '{"name":"NLP","parent_ids":[1]}'
curl "localhost:8000/api/entries?category_id=1&recursive=true"
```

## Run without Docker

```bash
pip install -r requirements.txt
DB_PATH=./data/catgraph.db uvicorn app:app --port 8000
```

## License

MIT — see [LICENSE](LICENSE). Use it, modify it, redistribute it, fork it as your own, no permission needed.

## Contributions

Feel free to fork and modify for your own needs. This repo doesn't review or accept pull requests.
