"""
Phase 2A — SQL for users, memberships, sessions, one-time tokens, login
attempts, audit events and stored original files.

Same rule as every repository module: organization-scoped tables are filtered
on organization_id inside the SQL. Tokens are only ever stored as SHA-256
hashes; the plaintext is never written here.
"""
from psycopg.types.json import Jsonb


# --- users ---------------------------------------------------------------------

def create_user(conn, email: str, display_name: str, password_hash: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, display_name, password_hash) VALUES (%s, %s, %s) RETURNING *",
            (email, display_name, password_hash),
        )
        return cur.fetchone()


def get_user_by_email(conn, email: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        return cur.fetchone()


def get_user(conn, user_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        return cur.fetchone()


def set_user_password(conn, user_id: str, password_hash: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET password_hash = %s, password_changed_at = now() WHERE id = %s",
                    (password_hash, user_id))


# --- memberships -----------------------------------------------------------------

def add_membership(conn, organization_id: str, user_id: str, role: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO memberships (organization_id, user_id, role) VALUES (%s, %s, %s)
               ON CONFLICT (organization_id, user_id) DO UPDATE SET role = EXCLUDED.role, status = 'active'
               RETURNING *""",
            (organization_id, user_id, role),
        )
        return cur.fetchone()


def get_membership(conn, organization_id: str, user_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM memberships WHERE organization_id = %s AND user_id = %s",
                    (organization_id, user_id))
        return cur.fetchone()


def list_user_memberships(conn, user_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT m.*, o.name AS organization_name FROM memberships m
               JOIN organizations o ON o.id = m.organization_id
               WHERE m.user_id = %s ORDER BY m.created_at""",
            (user_id,),
        )
        return cur.fetchall()


def list_org_members(conn, organization_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT m.id AS membership_id, m.role, m.status, m.created_at, u.id AS user_id, u.email,
                      u.display_name, u.is_active
               FROM memberships m JOIN users u ON u.id = m.user_id
               WHERE m.organization_id = %s ORDER BY u.email""",
            (organization_id,),
        )
        return cur.fetchall()


def update_membership(conn, organization_id: str, user_id: str, role: str | None, status: str | None) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE memberships SET role = COALESCE(%s, role), status = COALESCE(%s, status)
               WHERE organization_id = %s AND user_id = %s RETURNING *""",
            (role, status, organization_id, user_id),
        )
        return cur.fetchone()


def count_active_owners(conn, organization_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM memberships WHERE organization_id = %s AND role = 'OWNER' "
                    "AND status = 'active'", (organization_id,))
        return cur.fetchone()["n"]


# --- sessions --------------------------------------------------------------------

def create_session(conn, token_hash: str, user_id: str, organization_id: str, csrf_token: str,
                   expires_at, ip: str | None, user_agent: str | None) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO auth_sessions (token_hash, user_id, organization_id, csrf_token, expires_at, ip, user_agent)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (token_hash, user_id, organization_id, csrf_token, expires_at, ip, (user_agent or "")[:300]),
        )
        return cur.fetchone()


def get_session_by_hash(conn, token_hash: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM auth_sessions WHERE token_hash = %s", (token_hash,))
        return cur.fetchone()


def touch_session(conn, session_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE auth_sessions SET last_seen_at = now() WHERE id = %s", (session_id,))


def revoke_session(conn, session_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE auth_sessions SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL",
                    (session_id,))


def revoke_user_sessions(conn, user_id: str, organization_id: str | None = None) -> None:
    with conn.cursor() as cur:
        if organization_id:
            cur.execute("UPDATE auth_sessions SET revoked_at = now() WHERE user_id = %s AND organization_id = %s "
                        "AND revoked_at IS NULL", (user_id, organization_id))
        else:
            cur.execute("UPDATE auth_sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
                        (user_id,))


# --- one-time tokens ---------------------------------------------------------------

def create_auth_token(conn, kind: str, token_hash: str, organization_id: str, email: str, role: str | None,
                      user_id: str | None, created_by: str | None, expires_at) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO auth_tokens (kind, token_hash, organization_id, email, role, user_id, created_by, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (kind, token_hash, organization_id, email, role, user_id, created_by, expires_at),
        )
        return cur.fetchone()


def consume_auth_token(conn, kind: str, token_hash: str) -> dict | None:
    """Atomically marks a valid token used and returns it; None if it is
    unknown, expired, revoked or already used (single-use)."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE auth_tokens SET used_at = now()
               WHERE kind = %s AND token_hash = %s AND used_at IS NULL AND revoked_at IS NULL AND expires_at > now()
               RETURNING *""",
            (kind, token_hash),
        )
        return cur.fetchone()


def list_open_invitations(conn, organization_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, email, role, expires_at, created_at FROM auth_tokens
               WHERE organization_id = %s AND kind = 'invite' AND used_at IS NULL AND revoked_at IS NULL
                 AND expires_at > now() ORDER BY created_at DESC""",
            (organization_id,),
        )
        return cur.fetchall()


def revoke_auth_token(conn, organization_id: str, token_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("UPDATE auth_tokens SET revoked_at = now() WHERE id = %s AND organization_id = %s "
                    "AND used_at IS NULL AND revoked_at IS NULL RETURNING *", (token_id, organization_id))
        return cur.fetchone()


# --- login attempts ------------------------------------------------------------------

def record_login_attempt(conn, email: str | None, ip: str | None, succeeded: bool) -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO login_attempts (email, ip, succeeded) VALUES (%s, %s, %s)",
                    (email, ip, succeeded))


def recent_failed_attempts(conn, email: str | None, ip: str | None, minutes: int) -> tuple[int, int]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT
                 count(*) FILTER (WHERE email = %s) AS by_email,
                 count(*) FILTER (WHERE ip = %s) AS by_ip
               FROM login_attempts
               WHERE succeeded = FALSE AND created_at > now() - make_interval(mins => %s)""",
            (email, ip, minutes),
        )
        r = cur.fetchone()
        return r["by_email"], r["by_ip"]


# --- audit -----------------------------------------------------------------------------

def add_audit_event(conn, organization_id: str | None, actor_user_id: str | None, actor_email: str | None,
                    action: str, resource_type: str | None = None, resource_id: str | None = None,
                    metadata: dict | None = None, ip: str | None = None) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO audit_events (organization_id, actor_user_id, actor_email, action, resource_type,
                                         resource_id, metadata, ip)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (organization_id, actor_user_id, actor_email, action, resource_type,
             str(resource_id) if resource_id is not None else None, Jsonb(metadata or {}), ip),
        )
        return cur.fetchone()


def list_audit_events(conn, organization_id: str, limit: int = 50, offset: int = 0,
                      resource_type: str | None = None, resource_id: str | None = None,
                      action: str | None = None) -> list[dict]:
    clauses, params = ["organization_id = %s"], [organization_id]
    if resource_type:
        clauses.append("resource_type = %s")
        params.append(resource_type)
    if resource_id:
        clauses.append("resource_id = %s")
        params.append(resource_id)
    if action:
        clauses.append("action = %s")
        params.append(action)
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM audit_events WHERE {' AND '.join(clauses)} "
                    f"ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s", (*params, limit, offset))
        return cur.fetchall()


# --- stored files ------------------------------------------------------------------------

def save_stored_file(conn, organization_id: str, filename: str, content_type: str, sha256: str, data: bytes,
                     uploaded_by: str | None, document_id: str | None = None,
                     client_document_id: str | None = None, client_id: str | None = None) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO stored_files (organization_id, document_id, client_document_id, client_id, filename,
                                         content_type, sha256, size_bytes, data, uploaded_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id, organization_id, document_id, client_document_id, client_id, filename, content_type,
                         sha256, size_bytes, created_at""",
            (organization_id, document_id, client_document_id, client_id, filename, content_type, sha256,
             len(data), data, uploaded_by),
        )
        return cur.fetchone()


def find_duplicate_file(conn, organization_id: str, client_id: str | None, sha256: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, document_id, client_document_id, filename, created_at FROM stored_files
               WHERE organization_id = %s AND client_id IS NOT DISTINCT FROM %s AND sha256 = %s
               ORDER BY created_at LIMIT 1""",
            (organization_id, client_id, sha256),
        )
        return cur.fetchone()


def get_stored_file(conn, organization_id: str, *, document_id: str | None = None,
                    client_document_id: str | None = None) -> dict | None:
    with conn.cursor() as cur:
        if document_id:
            cur.execute("SELECT * FROM stored_files WHERE organization_id = %s AND document_id = %s",
                        (organization_id, document_id))
        else:
            cur.execute("SELECT * FROM stored_files WHERE organization_id = %s AND client_document_id = %s",
                        (organization_id, client_document_id))
        return cur.fetchone()


def list_stored_files_meta(conn, organization_id: str, client_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, document_id, client_document_id, filename, content_type, sha256, size_bytes, created_at
               FROM stored_files WHERE organization_id = %s AND client_id = %s""",
            (organization_id, client_id),
        )
        return cur.fetchall()
