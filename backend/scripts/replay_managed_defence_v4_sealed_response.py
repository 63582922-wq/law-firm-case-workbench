#!/usr/bin/env python3
"""Read-only replay of the fixed, one-shot managed-defence v4 response.

The V4 archive is a different immutable run from V3.  This small wrapper
deliberately accepts no arguments and only substitutes the three fixed V4
identifiers into the common sealed-response reader.  It never creates a run,
sends a provider request, stages a candidate, persists a document, or writes
an audit event.
"""

from __future__ import annotations

import json
import sys

import replay_managed_defence_v3_sealed_response as _sealed


_sealed._SEALED_RUN_ID = "7225a05e-8d6d-58ee-a0f9-7f646a4c0cb9"
_sealed._SEALED_MATTER_ID = "2ef22bd1-8179-507d-8ad8-5b5b20e0c4d5"
_sealed._SEALED_EXTERNAL_REQUEST_ID = "cc28061d-9295-51ad-b30d-d0ae0d5c7b37"
_sealed._SEALED_LABEL = "v4"


def main() -> int:
    if len(sys.argv) != 1:
        raise _sealed.SealedReplayBlocked("封存 v4 回放不接受参数，避免误指向其他案件。")
    print(json.dumps(_sealed._replay(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except _sealed.SealedReplayBlocked as error:
        print(
            json.dumps(
                {
                    "mode": "READ_ONLY_V4_SEALED_RESPONSE_REPLAY",
                    "status": "BLOCKED",
                    "reason": str(error),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        raise SystemExit(2)
