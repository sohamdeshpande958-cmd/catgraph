# CatGraph

A local store for **categories that form a graph (DAG, multiple parents)** and **entries that can belong to multiple categories**. SQLite storage, FastAPI backend, browser GUI. It runs fully offline.

## Run the prebuilt image

```bash
docker pull svind/catgraph:latest
docker run -d --name catgraph \
  -p 127.0.0.1:8008:8000 \
  -v ~/catgraph-data:/data \
  --restart unless-stopped \
  svind/catgraph:latest
```

Open **http://localhost:8008** for the GUI. The API docs are at **http://localhost:8008/docs**.

## Build from source

If you've cloned or forked this repo (and maybe changed the code), build it yourself:

```bash
docker compose up -d --build        # builds your local code, data goes in ./data
# or
docker build -t catgraph .
```

Both always build from the files in this folder. They never pull `svind/catgraph` from Docker Hub.

> **Your data lives in the mounted folder** (`~/catgraph-data/catgraph.db`, or `./data` with compose), not in the image.
> Without `-v`, data disappears when the container is removed. Back up by copying that file,
> or use **Export JSON** in the GUI.

## Using the GUI

- **Left panel:** the category graph. A category with several parents shows up under *each* parent, and a `×N` badge marks multi-parent categories. Click a category to filter entries. Double-click it to edit.
- **+ Category:** set a name, plus any number of parents and children. Cycles are rejected.
- **+ Entry:** set an optional ID of your own, a description, any number of categories, and optional JSON.
- **Include / Exclude filter** (above the table): add any number of categories to either row, by typing or with the **+ / −** buttons that appear when you hover a category in the tree. A plain click in the tree shows just that category. Your excludes stay, and **All entries** clears everything. The rules are listed under [Filtering](#filtering).
- **include sub-categories:** when checked, an included category also matches entries in all of its descendants.
- **Search** matches the description, your ID, and the JSON contents.
- **Table / Charts:** the Charts view describes the filtered entries.
  - *Entries by sub-category*: one bar per child of the current category. Click a bar to drill down, and use the breadcrumb to go back up.
  - *Categories per entry*: how many categories each entry has, and how many use only leaf categories.
- **Export CSV / Export JSON** (above the table): exports exactly what you're viewing, with the same filter and search. CSV has one row per entry, with all of its categories. JSON includes the category sub-graph and can be re-imported.
- **Full backup / Import** (header): the whole database. On import you choose between replace and merge.

## Filtering

An entry is **under X** when one of its categories is X or a descendant of X, through any path in the DAG.

| control | keeps / hides |
|---|---|
| Include, **ANY** | keeps entries under at least one included category |
| Include, **ALL** | keeps entries under every included category |
| Exclude, **strict** (default) | hides every entry under X, even one that also sits under another branch. This is the exact opposite of including X. |
| Exclude, **inside** (click the chip's `strict` label to switch) | hides an entry only if *all* of its categories are under X |

The same filter works on `/api/entries`, `/api/export` and `/api/stats`:

```bash
# entries under LLMs or Vision Transformers, minus anything under Healthcare
curl "localhost:8000/api/entries?include=52,53&exclude=20"
# both at once, and drop entries that live entirely inside Leaderboards
curl "localhost:8000/api/entries?include=52,53&mode=all&exclude_inside=51"
# chart data: per-child counts under category 25, plus categories-per-entry
curl "localhost:8000/api/stats?group_by=25&exclude=20"
curl "localhost:8000/api/export?include=52&format=csv"
```

Parameters: `include`, `exclude` and `exclude_inside` take comma-separated category ids. `mode` is `any` or `all`. `recursive=false` makes includes match only direct tags. `q` is the text search. The older `category_id=5` still works and means `include=5`.

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

## Maintainer: publishing to Docker Hub

```bash
docker build -t svind/catgraph:latest .
docker login
docker push svind/catgraph:latest
```

The Docker Hub overview text lives in [DOCKERHUB.md](DOCKERHUB.md). Paste it into the repo's Overview tab on Docker Hub.

## License

MIT — see [LICENSE](LICENSE). Use it, modify it, redistribute it, fork it as your own, no permission needed.

## Contributions

Feel free to fork and modify for your own needs. This repo doesn't review or accept pull requests.
