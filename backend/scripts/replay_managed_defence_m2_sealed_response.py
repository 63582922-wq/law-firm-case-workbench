#!/usr/bin/env python3
"""Read-only replay of the one recorded M2 provider response.

M2 is a fixed, single-call synthetic acceptance run.  This wrapper accepts no
arguments and substitutes only its immutable identifiers into the common
sealed-response reader.  It cannot create a run, make a provider call, stage
a candidate, persist a document, or write an audit event.
"""

from __future__ import annotations

import json
import sys

import replay_managed_defence_v3_sealed_response as _sealed


_sealed._SEALED_RUN_ID = "a125043a-e087-53e8-bb6e-84ee8f46579c"
_sealed._SEALED_MATTER_ID = "53bb72e1-06a9-508f-b16d-de588e2fc3ff"
_sealed._SEALED_EXTERNAL_REQUEST_ID = "b3e15af1-3883-5fdd-bf54-3f9492e07c54"
_sealed._SEALED_LABEL = "m2"


def main() -> int:
    if len(sys.argv) != 1:
        raise _sealed.SealedReplayBlocked("封存 M2 回放不接受参数，避免误指向其他案件。")
    print(
        json.dumps(
            _sealed._replay(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except _sealed.SealedReplayBlocked as error:
        print(
            json.dumps(
                {
                    "mode": "READ_ONLY_M2_SEALED_RESPONSE_REPLAY",
                    "status": "BLOCKED",
                    "reason": str(error),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        raise SystemExit(2)
