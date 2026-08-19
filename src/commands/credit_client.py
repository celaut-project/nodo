from src.database.sql_connection import SQLConnection
from src.manager.manager import ALLOW_DEBT
from src.utils.monetary import display_unit, format_mu, parse_to_mu

sc = SQLConnection()


def credit_client(client_id: str, amount: str, decrement: bool = False):
    """Credit or debit a client's balance.

    The amount is in the operator's display unit (`ui.DISPLAY_UNIT`, ERG by default) --
    what they read everywhere else. MU is the integer unit the node counts in and is
    never asked for here.
    """
    try:
        amount_mu = parse_to_mu(amount)
    except ValueError as exc:
        print(f"Invalid amount: {exc}")
        return

    if amount_mu == 0:
        print(f"Nothing to modify: {amount} {display_unit().symbol} is zero.")
        return

    balance = sc.get_client_balance(client_id)
    if balance is None:
        print(f"Client not found: {client_id}")
        return

    if decrement:
        current_mu, _, _ = balance
        # Same guard as automatic billing (`spend_mu`, `costs.ALLOW_DEBT`): a manual
        # debit is still a debit, so it is refused past zero on the same terms.
        if current_mu < amount_mu and not ALLOW_DEBT:
            print(
                f"Insufficient balance for client {client_id}: "
                f"{format_mu(current_mu)} available, needed {format_mu(amount_mu)}."
            )
            return
        sc.reduce_balance(client_id, amount_mu)
        verb = "debited"
    else:
        sc.add_balance(client_id, amount_mu)
        verb = "credited"

    print(f"Client {client_id} {verb} {format_mu(amount_mu)}.")
