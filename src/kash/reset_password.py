"""Reset the login password from the server (there is no "forgot password" e-mail).

    python -m kash.reset_password                  # the owner; asks for the new password twice
    python -m kash.reset_password --email me@x.com  # also change the owner's login email
    python -m kash.reset_password --login sam@x.com # someone else in the household
    echo 'new-password' | python -m kash.reset_password --password-stdin

Docker / Unraid: open the kash container's console (or ``docker exec -it kash sh``)
and run the first command. It uses the container's own database settings. Every
session of that person is logged out, because sessions are tied to the password.
(The owner can also make a "Password link" for others in Settings → People.)
"""

import argparse
import asyncio
import getpass
import sys
from collections.abc import Callable

from kash.config import get_settings
from kash.db.repositories.provider import DatabaseDataProvider
from kash.db.session import create_engine_and_factory, init_db
from kash.domain.protocols import DataProvider
from kash.owner import OWNER_USER_ID
from kash.services import auth


class ResetError(Exception):
    """Shown to the person running the command; exit code 1."""


async def reset_password(
    provider: DataProvider, password: str, email: str | None = None, login: str | None = None
) -> str:
    """Set a new password (and optionally email) for the owner, or for the person whose
    login is ``login``. Returns the login email."""
    owner = await provider.users.get_user(OWNER_USER_ID)
    if owner is None or not auth.has_account(owner.hashed_password):
        raise ResetError("No account yet: open Kash in a browser to create one.")
    user = await provider.users.get_user_by_email(login) if login else owner
    if user is None:
        raise ResetError(f"No one logs in as {login}.")
    if len(password) < auth.MIN_PASSWORD_LENGTH:
        raise ResetError(f"Use at least {auth.MIN_PASSWORD_LENGTH} characters.")
    new_email = (email or user.email).strip().lower()
    if "@" not in new_email:
        raise ResetError("That email address doesn't look right.")
    other = await provider.users.get_user_by_email(new_email)
    if other is not None and other.id != user.id:
        raise ResetError(f"{new_email} is someone else's login.")
    await provider.users.save_credentials(user.id, new_email, auth.hash_password(password))
    return new_email


def _read_password(from_stdin: bool, ask: Callable[[str], str] = getpass.getpass) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = ask("New password: ")
    if first != ask("New password again: "):
        raise ResetError("The passwords don't match.")
    return first


async def _run(password: str, email: str | None, login: str | None) -> str:
    engine, factory = create_engine_and_factory(get_settings().database_url)
    try:
        await init_db(engine)  # same schema the app expects (no-op when current)
        return await reset_password(DatabaseDataProvider(factory), password, email, login)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m kash.reset_password",
        description="Set a new Kash login password (logs out that person's sessions).",
    )
    parser.add_argument("--email", help="also change the login email to this address")
    parser.add_argument(
        "--login", metavar="EMAIL", help="whose password to reset (default: the owner)"
    )
    parser.add_argument(
        "--password-stdin", action="store_true", help="read the new password from standard input"
    )
    args = parser.parse_args(argv)
    try:
        password = _read_password(args.password_stdin)
        login = asyncio.run(_run(password, args.email, args.login))
    except ResetError as exc:
        print(f"Not changed: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nNot changed.", file=sys.stderr)
        return 1
    print(f"Password changed. Log in as {login}. Their other sessions are logged out.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
