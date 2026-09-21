"""JSON-line adapter for the existing migration smoke test, fresh local PG16 only."""
import json
from pathlib import Path
import re
import sys

import psycopg
from psycopg.rows import dict_row

root = Path(__file__).resolve().parents[3]
socket = Path(sys.argv[1]).resolve(strict=True)
if not socket.is_relative_to(root / "artifacts") or socket.stat().st_mode & 0o077:
    raise SystemExit("native test socket must be inside project artifacts")
with psycopg.connect(host=str(socket), port=5432, dbname="postgres", user="postgres",
                     options="-c search_path=public", connect_timeout=5,
                     autocommit=True, row_factory=dict_row) as connection:
    identity = connection.execute("SELECT current_setting('data_directory') AS path, current_setting('server_version_num')::int AS version, current_setting('listen_addresses') AS listen").fetchone()
    if not (160000 <= identity["version"] < 170000 and
            identity["listen"] == "" and
            Path(identity["path"]).resolve().is_relative_to(root / "artifacts" / "native-pg16")):
        raise SystemExit("native test requires isolated project PG16 data directory")
    if connection.execute("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname !~ '^pg_' AND rolname <> 'postgres') OR EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public') AS populated").fetchone()["populated"]:
        raise SystemExit("native smoke refuses an existing application cluster")
    for line in sys.stdin:
        try:
            request = json.loads(line)
            parameters = request.get("params")
            sql = request["sql"]
            if parameters is not None:
                bound = []
                def parameter(match):
                    bound.append(parameters[int(match[1]) - 1])
                    return "%s"
                sql = re.sub(r"\$(\d+)", parameter, sql)
                cursor = connection.execute(sql, bound)
            else:
                cursor = connection.execute(sql)
            result = {"rows": cursor.fetchall() if request["query"] else []}
        except Exception as error:
            result = {"error": str(error), "code": getattr(error, "sqlstate", None)}
        print(json.dumps(result, default=str), flush=True)
