import logging, os
from decimal import Decimal, getcontext

from src.utils.env import EnvManager

env_manager = EnvManager()
STORAGE, USE_PRINT = env_manager.get_env("STORAGE"), env_manager.get_env("USE_PRINT")

if not os.path.exists(STORAGE):
    os.makedirs(STORAGE)

logging.basicConfig(
    filename=f'{STORAGE}/app.log',
    level=logging.INFO,
    format='%(message)s'
)

LOGGER = (
    lambda message: print(message + '\n')
) if USE_PRINT else (
    lambda message: logging.getLogger(__name__).info(message + '\n')
)


def ssformat(number, sig_digits=3):
    """
    Smart Scientific Format with residual using Decimal.

    Args:
      number: Decimal or str (e.g., "12.0000000...00748")
      sig_digits: number of significant digits in the main mantissa.

    Returns:
      '1.2e+1 + 7.48e-34' or just '1.20e+1' if there's no residual.
    """
    # 1) Ensure we're working with Decimal constructed from a string
    if not isinstance(number, Decimal):
        number = Decimal(str(number))
    if number == 0:
        return "0"

    # 2) Set context precision to avoid losing digits in the residual
    #    Set high precision: all digits + margin
    total_digits = len(number.as_tuple().digits)
    getcontext().prec = max(total_digits + 5, sig_digits + 5)

    # 3) Decompose into mantissa and exponent:
    #    number = mantissa * 10**exponent, with mantissa in [1,10)
    exp = number.normalize().as_tuple().exponent
    digits = number.normalize().as_tuple().digits
    # The true exponent is:
    exponent = exp + len(digits) - 1
    # Build the full mantissa as a Decimal
    mant_full = number.scaleb(-exponent)

    # 4) Round main mantissa to sig_digits
    mant_main = mant_full.quantize(Decimal(1).scaleb(-(sig_digits-1)))
    # Format by removing trailing zeros
    mant_str = format(mant_main.normalize(), 'f').rstrip('0').rstrip('.') 

    # 5) Calculate residual
    residual = number - mant_main * (Decimal(10) ** exponent)
    if residual == 0:
        return f"{mant_str}e{exponent:+d}"
    else:
        # Exponent and mantissa of the residual
        r_norm = residual.normalize()
        r_exp = r_norm.as_tuple().exponent + len(r_norm.as_tuple().digits) - 1
        r_mant = r_norm.scaleb(-r_exp)
        r_str = format(r_mant.normalize(), 'f').rstrip('0').rstrip('.')
        return f"{mant_str}e{exponent:+d} + {r_str}e{r_exp:+d}"
