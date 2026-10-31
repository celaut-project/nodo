import os


def db_reset_command(database_file: str, assume_yes: bool = False) -> bool:
    """Delete the database so the next start (or `nodo migrate`) makes an empty one.

    The one place that deletes it, and no setup or update script calls it: the database
    holds peers, clients, balances, reputation and donation debts, none of which can be
    rebuilt (#518). It refuses while a node is serving, which would keep writing to a
    file that is no longer there.
    """
    from src.commands.daemon import is_serving

    if is_serving():
        print("A node is serving. Stop it first (`nodo daemon stop`).", flush=True)
        return False
    if not os.path.exists(database_file):
        print(f"There is no database at {database_file}.", flush=True)
        return True
    if not assume_yes:
        print(f"This deletes {database_file}: peers, clients, balances, reputation and "
              "donation debts. It cannot be undone.", flush=True)
        if input("Type 'reset' to continue: ").strip() != "reset":
            print("Cancelled.", flush=True)
            return False
    os.remove(database_file)
    print("Database deleted. `nodo migrate` makes an empty one.", flush=True)
    return True
