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

## Instance manager: one database per topic

`catgraph_manager.py` is a terminal menu for running several CatGraph instances side by side, for example one per topic you take notes on. Each instance is its own container on its own port, with its own SQLite file at `<DATA_ROOT>/<name>/catgraph.db` on your machine. You can remove and recreate containers without touching the data.

It needs only Python 3.8+ and the Docker CLI:

```bash
python3 catgraph_manager.py
```

**Setup.** The image settings are blank on purpose, so open the script and fill in the configuration block at the top with your own values first:

| setting | what to put there |
|---|---|
| `IMAGE` | the image tag to spawn from: `svind/catgraph:latest` if you pulled it, `catgraph:latest` if you built with compose, or your own tag. Required unless `EXPECTED_IMAGE_ID` is set. |
| `EXPECTED_IMAGE_ID` | optional. The short ID of a build you trust (the `IMAGE ID` column of `docker images`). If the tag later points at a different build, the script warns you and lists containers from both builds. |
| `DATA_ROOT` | the folder that holds one sub-folder per instance |
| `DEFAULT_PORT` | the first host port tried for a new instance. The script picks the next free one from there, and you can override it when spawning. |
| `BIND_HOST` | `127.0.0.1` makes instances reachable only from this machine. `0.0.0.0` makes them reachable from your network, and the menu flags such instances as exposed. |

**Menu:**

1. **Spawn** a new instance: pick a name and a port. If `<DATA_ROOT>/<name>` already holds a database, it is reused.
2. **Start** or 3. **stop** an existing instance.
4. **Delete a container** and keep its data. Spawn the same name again later to reconnect.
5. **Delete an instance and its data.** You have to type the name to confirm.
6. **Refresh** the list.

**Good to know:**
- It lists every container made from your image, whether it was created by this script, by compose or by hand.
- Data folders with no container are listed too, so you can bring them back.
- It warns before two containers would write to the same database file at once, because that can corrupt SQLite.
- It only ever deletes folders inside `DATA_ROOT`. Files the container created as root are removed through a throwaway container, so you never need sudo.

**Moving instances to a new build:** rebuild or pull the image, and update `EXPECTED_IMAGE_ID` (or leave it blank). Then, for each instance, use option 4 and spawn it again with the same name. The new container picks up the existing database.

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
- **Add to categories…** (above the table): tags *every* entry matching the current filter and search with one or more extra categories. That means all matching entries, not just the 500 rows the table shows. Entries keep their existing categories, and links that already exist are skipped. It asks for confirmation when no filter is set or more than 500 entries match.
- **Full backup / Import** (header): the whole database. On import you choose between replace and merge.

## Filtering

An entry is **under X** when one of its categories is X or a descendant of X, through any path in the DAG.

| control | keeps / hides |
|---|---|
| Include, **ANY** | keeps entries under at least one included category |
| Include, **ALL** | keeps entries under every included category |
| Exclude, **strict** (default) | hides every entry under X, even one that also sits under another branch. This is the exact opposite of including X. |
| Exclude, **inside** (click the chip's `strict` label to switch) | hides an entry only if *all* of its categories are under X |

The same filter works on `/api/entries`, `/api/export`, `/api/stats` and `/api/entries/add-categories`:

```bash
# entries under LLMs or Vision Transformers, minus anything under Healthcare
curl "localhost:8000/api/entries?include=52,53&exclude=20"
# both at once, and drop entries that live entirely inside Leaderboards
curl "localhost:8000/api/entries?include=52,53&mode=all&exclude_inside=51"
# chart data: per-child counts under category 25, plus categories-per-entry
curl "localhost:8000/api/stats?group_by=25&exclude=20"
curl "localhost:8000/api/export?include=52&format=csv"
# also tag everything under LLMs (minus Healthcare) with categories 7 and 9; returns {"matched": …, "added": …}
curl -X POST "localhost:8000/api/entries/add-categories?include=52&exclude=20" \
  -H 'Content-Type: application/json' -d '{"category_ids":[7,9]}'
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
