"""
Phase 2A — first-account setup from the server (v0.14.0).

    python3 manage_users.py create-owner --email you@firm.in --name "Your Name" --org "Your Brokerage"
    python3 manage_users.py create-owner --email you@firm.in --name "Your Name" --org-id <existing org id>
    python3 manage_users.py set-password --email you@firm.in

The password is asked for interactively (never on the command line, so it is
not kept in shell history) — or read from AUREVIA_NEW_PASSWORD for
non-interactive setups. Every other account is created in the app by an
Owner/Admin invitation.
"""
import argparse
import getpass
import os
import sys

import auth
import db


def _password() -> str:
    pw = os.environ.get("AUREVIA_NEW_PASSWORD")
    if pw:
        return pw
    pw = getpass.getpass("New password (min 10 characters): ")
    if pw != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match.")
    return pw


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-owner")
    c.add_argument("--email", required=True)
    c.add_argument("--name", required=True)
    g = c.add_mutually_exclusive_group(required=True)
    g.add_argument("--org")
    g.add_argument("--org-id")
    s = sub.add_parser("set-password")
    s.add_argument("--email", required=True)
    a = p.parse_args()
    db.init_schema()
    try:
        email = auth.normalize_email(a.email)
        pw_hash = auth.hash_password(_password())
    except Exception as exc:                       # HTTPException from validation
        sys.exit(getattr(exc, "detail", str(exc)))
    with db.get_connection() as conn:
        if a.cmd == "create-owner":
            if a.org_id:
                org = db.get_organization(conn, a.org_id)
                if org is None:
                    sys.exit("No organization with that id.")
            else:
                org = db.create_organization(conn, a.org.strip())
            user = db.get_user_by_email(conn, email)
            if user is None:
                user = db.create_user(conn, email, " ".join(a.name.split()), pw_hash)
            else:
                db.set_user_password(conn, str(user["id"]), pw_hash)
            db.add_membership(conn, str(org["id"]), str(user["id"]), "OWNER")
            db.add_audit_event(conn, str(org["id"]), str(user["id"]), email, "membership.owner_created_cli",
                               "user", str(user["id"]), {})
            print(f"Owner {email} ready in organization '{org['name']}' ({org['id']}).")
        else:
            user = db.get_user_by_email(conn, email)
            if user is None:
                sys.exit("No user with that e-mail.")
            db.set_user_password(conn, str(user["id"]), pw_hash)
            db.revoke_user_sessions(conn, str(user["id"]))
            db.add_audit_event(conn, None, str(user["id"]), email, "auth.password_set_cli", "user",
                               str(user["id"]), {})
            print(f"Password updated for {email}; existing sessions signed out.")


if __name__ == "__main__":
    main()
