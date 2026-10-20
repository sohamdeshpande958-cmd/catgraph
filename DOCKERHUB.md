# CatGraph

A local store for **categories that form a graph (DAG, multiple parents)** and **entries that can belong to multiple categories**. SQLite storage, FastAPI backend, browser GUI. It runs fully offline.

Source, full docs and schema: https://github.com/sohamdeshpande958-cmd/catgraph

## Quick start

```bash
docker run -d --name catgraph \
  -p 127.0.0.1:8008:8000 \
  -v ~/catgraph-data:/data \
  --restart unless-stopped \
  svind/catgraph:latest
```

- GUI: http://localhost:8008
- API docs: http://localhost:8008/docs

> **Mount `/data`.** The database lives at `/data/catgraph.db`. Without `-v`, your data is lost when the container is removed.
> Back up by copying that file, or use **Export JSON** in the GUI.

## Compose

```yaml
services:
  catgraph:
    image: svind/catgraph:latest
    ports:
      - "127.0.0.1:8008:8000"
    volumes:
      - ./data:/data
    restart: unless-stopped
```

## Reference

| | |
|---|---|
| Port | `8000` (bind to `127.0.0.1`: there is no authentication) |
| Volume | `/data` |
| Env | `DB_PATH` (default `/data/catgraph.db`) |
| Healthcheck | `GET /api/health` (built in) |

## License

MIT © Soham Deshpande. The license text ships in the image at `/app/LICENSE`, and is also in the [GitHub repo](https://github.com/sohamdeshpande958-cmd/catgraph/blob/main/LICENSE).
