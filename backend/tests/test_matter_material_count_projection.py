from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4
import unittest
from unittest.mock import patch

from case_kernel.models import Actor, Role
from case_kernel.postgres_store import PostgresMatterStore


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _ListConnection:
    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, statement, parameters):
        self.calls.append((str(statement), parameters))
        return _Rows(self.rows)


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, traceback):
        return False


class MatterMaterialCountProjectionTests(unittest.TestCase):
    def test_image_bridge_is_counted_once_by_the_authoritative_material_row(self) -> None:
        firm_id, actor_id, matter_id = str(uuid4()), str(uuid4()), str(uuid4())
        actor = Actor(actor_id, firm_id, frozenset({Role.LEAD_LAWYER}))
        connection = _ListConnection(
            [{
                "matter_id": matter_id,
                "title": "Synthetic image-only matter",
                "stage": "CREATED",
                "version": 2,
                "updated_at": datetime.now(timezone.utc),
                "material_count": 1,
            }]
        )
        store = PostgresMatterStore("postgresql://not-used.invalid/lawcase_test")

        with patch.object(store, "_transaction", return_value=_Transaction(connection)):
            projection = store.list_accessible(actor=actor)

        self.assertEqual(1, projection[0]["material_count"])
        query, parameters = connection.calls[0]
        self.assertIn("FROM case_material_objects material", query)
        self.assertNotIn("material.admitted_format", query)
        self.assertIn("evidence.media_type = 'application/pdf'", query)
        self.assertEqual((firm_id, actor_id), parameters)


if __name__ == "__main__":
    unittest.main()
