#!/usr/bin/env python3
"""Render the M2 sealed response into an ephemeral lawyer-review document.

This is a fixed, no-argument wrapper around the shared sealed-response
document renderer.  It makes no model request and does not change the M2 run,
case records, object store or court-submission state.
"""

from __future__ import annotations

import sys

import render_managed_defence_v4_sealed_candidate as _renderer
import replay_managed_defence_m2_sealed_response as _m2


# Importing the M2 wrapper after the V4 renderer restores the intentionally
# fixed M2 identifiers in the common sealed-response reader for this process.
_renderer._sealed = _m2._sealed


def main() -> int:
    if len(sys.argv) != 1:
        raise _renderer.SealedDocumentReplayBlocked(
            "封存 M2 文书回放不接受参数，避免误指向其他案件。"
        )
    print(_renderer._json_text(_renderer._replay()))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (_renderer.SealedDocumentReplayBlocked, _m2._sealed.SealedReplayBlocked) as error:
        print(
            _renderer._json_text(
                {
                    "mode": "READ_ONLY_M2_SEALED_RESPONSE_DOCUMENT_REPLAY",
                    "status": "BLOCKED",
                    "reason": str(error),
                }
            )
        )
        raise SystemExit(2)
