from src.manager.manager import modify_deposit
from src.utils.monetary import erg_to_mu, mu_to_erg_str


def modify_instance_deposit(instance: str, erg: str, decrement: bool = False):
    """Move ERG into or out of an instance's deposit.

    The amount is ERG because that is what the operator holds and what the node's
    prices are quoted in; MU is the integer unit the node counts in internally and is
    never asked for here.
    """
    try:
        amount_mu = erg_to_mu(erg)
    except ValueError as exc:
        print(f"Invalid ERG amount: {exc}")
        return

    if amount_mu == 0:
        print("Nothing to modify: the amount is zero.")
        return

    if decrement:
        amount_mu *= -1

    result, msg = modify_deposit(amount_mu=amount_mu, service_token=instance)
    verb = "decreased" if decrement else "increased"
    if result:
        print(f"Deposit of instance {instance} {verb} by {mu_to_erg_str(abs(amount_mu))} ERG.")
    else:
        print(f"Something was wrong: {msg}.")
