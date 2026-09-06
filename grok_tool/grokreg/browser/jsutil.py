"""Shared JS execution helpers for pydoll tabs (used by chrome + page_flow)."""
from __future__ import annotations

import json
from typing import Any


def _unwrap_js_result(result: Any) -> Any:
    """Normalize pydoll/CDP evaluate return values."""
    if not isinstance(result, dict):
        return result
    # CDP shape: {id, result: {result: {type, value}}}
    try:
        inner = result
        for _ in range(6):
            if not isinstance(inner, dict):
                break
            # terminal CDP remote object
            if "type" in inner and ("value" in inner or inner.get("type") == "undefined"):
                val = inner.get("value")
                # auto-parse JSON strings we wrap ourselves
                if isinstance(val, str):
                    s = val.strip()
                    if (s.startswith("{") and s.endswith("}")) or (
                        s.startswith("[") and s.endswith("]")
                    ):
                        try:
                            return json.loads(s)
                        except Exception:
                            pass
                return val
            if "result" in inner:
                inner = inner["result"]
                continue
            if "value" in inner and len(inner) <= 3:
                return inner["value"]
            break
        return result
    except Exception:
        return result


async def _exec_js(tab: Any, script: str) -> Any:
    """
    Execute JS and return a Python value.
    pydoll returns objectId for objects unless return_by_value=True;
    we also JSON.stringify complex expressions as fallback.
    """
    # Prefer scripts that already stringify; otherwise wrap when needed
    script_stripped = script.strip()
    candidates = [script_stripped]
    # If it's an IIFE returning object, also try stringify wrap
    if script_stripped.startswith("(()") or script_stripped.startswith("(function"):
        candidates.append(
            f"(() => {{ const __r = ({script_stripped}); "
            f"try {{ return JSON.stringify(__r); }} catch (e) {{ return __r; }} }})()"
        )

    for method_name in ("execute_script", "evaluate"):
        if not hasattr(tab, method_name):
            continue
        fn = getattr(tab, method_name)
        for sc in candidates:
            try:
                try:
                    raw = await fn(sc, return_by_value=True)
                except TypeError:
                    raw = await fn(sc)
                val = _unwrap_js_result(raw)
                # skip useless objectId-only remote objects
                if isinstance(val, dict) and set(val.keys()) <= {
                    "id",
                    "result",
                    "type",
                    "className",
                    "description",
                    "objectId",
                }:
                    continue
                return val
            except Exception:
                continue
    return None
