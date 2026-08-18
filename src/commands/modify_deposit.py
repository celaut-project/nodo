from src.manager.manager import modify_deposit
from src.utils.monetary import display_unit, format_mu, parse_to_mu


def modify_instance_deposit(instance: str, amount: str, decrement: bool = False):
    """Move funds into or out of an instance's deposit.

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

    if decrement:
        amount_mu *= -1

    result, msg = modify_deposit(amount_mu=amount_mu, service_token=instance)
    verb = "decreased" if decrement else "increased"
    if result:
        print(f"Deposit of instance {instance} {verb} by {format_mu(abs(amount_mu))}.")
    else:
        print(f"Something was wrong: {msg}.")
