"""Privacy-safe audit recording with encrypted sensitive detail payloads."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Mapping

import crypto_store


COLUMNS_DOMAIN = "hstagent/columns/v1"
_REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|bearer|token|secret|password|passwd|api[_-]?key|"
    r"private[_-]?key|sub[_-]?id|card[_-]?number|credential|cookie)",
    re.IGNORECASE,
)
_TEXT_REDACTIONS = (
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE), "Bearer [REDACTED]"),
    (
        re.compile(
            r"([?&](?:access_token|refresh_token|token|api_key|key|secret|password)=)[^&#\s]+",
            re.IGNORECASE,
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"(/(?:sub|subscription|token|secret)/)[^/?#\s]+", re.IGNORECASE),
        r"\1[REDACTED]",
    ),
)


class AuditServiceError(RuntimeError):
    """Safe audit failure without source values or ciphertext in the message."""


def redact_sensitive(value: Any) -> Any:
    """Recursively redact credential-bearing keys and tokenized text."""
    if isinstance(value, Mapping):
        cleaned = {}
        for key, item in value.items():
            key_text = str(key)
            cleaned[key_text] = _REDACTED if _SENSITIVE_KEY.search(key_text) else redact_sensitive(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        result = value
        for pattern, replacement in _TEXT_REDACTIONS:
            result = pattern.sub(replacement, result)
        return result
    return value


def _metadata(action: str, resource: Any, result_status: str, persona: Any) -> str:
    metadata = {"action": action, "result": result_status}
    if resource is not None:
        metadata["resource"] = redact_sensitive(resource)
    if persona is not None:
        metadata["actor_persona"] = redact_sensitive(persona)
    return json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)


def _insert(
    conn: sqlite3.Connection,
    *,
    rep_id: int | None,
    account_code: str | None,
    email: str | None,
    action: str,
    actor_id: int,
    actor_name: str | None,
    details_json: str,
    result_status: str,
    details_cipher: str | None,
) -> int:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(audit_logs)")}
    if "details_cipher" not in columns:
        raise AuditServiceError("audit schema is missing encrypted detail storage")
    cursor = conn.execute(
        "INSERT INTO audit_logs(rep_id,account_code,email,action,actor_id,actor_name,"
        "details_json,result_status,details_cipher) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            rep_id,
            str(account_code or ""),
            str(email or ""),
            action,
            int(actor_id),
            actor_name,
            details_json,
            result_status,
            details_cipher,
        ),
    )
    return int(cursor.lastrowid)


def record_audit(
    action: str,
    actor_id: int,
    actor_name: str | None = None,
    details: Any = None,
    *,
    rep_id: int | None = None,
    account_code: str | None = None,
    email: str | None = None,
    result_status: str = "success",
    resource: Any = None,
    persona: Any = None,
    db_path: str | None = None,
    connection: sqlite3.Connection | None = None,
) -> int:
    """Persist metadata separately from an authenticated encrypted details payload.

    actor_id/actor_name represent the real actor; persona is a separate,
    sanitized metadata field and is never substituted for the real identity.
    A caller-supplied connection remains owned by that caller.
    """
    if not isinstance(action, str) or not action.strip():
        raise AuditServiceError("audit action is required")
    if actor_id is None:
        raise AuditServiceError("real actor identity is required")

    try:
        details_cipher = None
        if details is not None:
            raw_details = json.dumps(details, ensure_ascii=False, sort_keys=True, default=str)
            details_cipher = crypto_store.encrypt_secret(raw_details, COLUMNS_DOMAIN)
        safe_metadata = _metadata(action.strip(), resource, str(result_status), persona)

        if connection is not None:
            return _insert(
                connection,
                rep_id=rep_id,
                account_code=account_code,
                email=email,
                action=action.strip(),
                actor_id=int(actor_id),
                actor_name=actor_name,
                details_json=safe_metadata,
                result_status=str(result_status),
                details_cipher=details_cipher,
            )

        if db_path is not None:
            with sqlite3.connect(db_path, timeout=30) as conn:
                return _insert(
                    conn,
                    rep_id=rep_id,
                    account_code=account_code,
                    email=email,
                    action=action.strip(),
                    actor_id=int(actor_id),
                    actor_name=actor_name,
                    details_json=safe_metadata,
                    result_status=str(result_status),
                    details_cipher=details_cipher,
                )

        import database

        with database.get_db() as conn:
            return _insert(
                conn,
                rep_id=rep_id,
                account_code=account_code,
                email=email,
                action=action.strip(),
                actor_id=int(actor_id),
                actor_name=actor_name,
                details_json=safe_metadata,
                result_status=str(result_status),
                details_cipher=details_cipher,
            )
    except AuditServiceError:
        raise
    except Exception:
        raise AuditServiceError("audit event could not be recorded") from None
