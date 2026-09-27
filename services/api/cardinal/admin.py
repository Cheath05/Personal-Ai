"""Recovery commands, run on the hub itself (SSH in first: `ssh usr1@cardinal`).

    cd ~/Personal-Ai/services/api
    .venv/bin/python -m cardinal.admin status     # is the lock on, how many passkeys and signed-in browsers
    .venv/bin/python -m cardinal.admin code       # a one-time code to add a passkey on a new device
    .venv/bin/python -m cardinal.admin unlock     # turn the passkey lock off (e.g. every passkey was lost)
    .venv/bin/python -m cardinal.admin sign-out   # sign every browser out

Being able to SSH into the hub is the proof that it's you: Tailscale only lets your own devices in.
"""

import sys

from sqlmodel import Session, select

from . import prefs
from .auth import locked, new_code
from .db import LoginSession, Passkey, get_engine


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    with Session(get_engine()) as session:
        if cmd == "status":
            print(f"Lock: {'on' if locked(session) else 'off'}")
            print(f"Passkeys: {', '.join(p.name for p in session.exec(select(Passkey)).all()) or 'none'}")
            print(f"Signed-in browsers: {len(session.exec(select(LoginSession)).all())}")
        elif cmd == "code":
            code, expires = new_code(session)
            print(f"One-time code: {code}  (works once, until {expires:%H:%M} UTC)")
            print("On the new device: open Cardinal → Use a one-time code → type it → make a passkey.")
        elif cmd == "unlock":
            prefs.put(session, "login_required", "0")
            print("The lock is off. Add a passkey in Access → Security, then turn it back on.")
        elif cmd == "sign-out":
            for s in session.exec(select(LoginSession)).all():
                session.delete(s)
            session.commit()
            print("Every browser is signed out.")
        else:
            print(__doc__)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
