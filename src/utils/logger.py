import logging, os
from decimal import Decimal, getcontext, InvalidOperation

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
      number: Decimal, int/float, or str containing a valid decimal literal
              (e.g. "12.00000000000000748")
      sig_digits: number of significant digits in the main mantissa.

    Returns:
      '1.2e+1 + 7.48e-34' or just '1.20e+1' if there's no residual.
    """
    # 1) Convert into a Decimal, with special handling for strings
    if isinstance(number, str):
        # strip underscores and whitespace
        cleaned = number.replace('_', '').strip()
        try:
            number = Decimal(cleaned)
        except InvalidOperation:
            raise ValueError(f"Could not parse '{number}' as a Decimal")
    elif not isinstance(number, Decimal):
        # for ints/floats: go via str() to avoid binary-float issues
        number = Decimal(str(number))

    if number == 0:
        return "0"

    # 2) Boost precision so we don't lose any residual digits
    total_digits = len(number.normalize().as_tuple().digits)
    getcontext().prec = max(total_digits + 5, sig_digits + 5)

    # 3) Decompose: number = mant_full * 10**exponent
    norm = number.normalize()
    exp   = norm.as_tuple().exponent
    digits = norm.as_tuple().digits
    exponent = exp + len(digits) - 1
    mant_full = number.scaleb(-exponent)

    # 4) Round the main mantissa
    mant_main = mant_full.quantize(Decimal(1).scaleb(-(sig_digits-1)))
    mant_str  = format(mant_main.normalize(), 'f').rstrip('0').rstrip('.')

    # 5) Compute residual
    residual = number - mant_main * (Decimal(10) ** exponent)
    if residual == 0:
        return f"{mant_str}e{exponent:+d}"
    else:
        # decompose residual
        r_norm = residual.normalize()
        r_exp  = r_norm.as_tuple().exponent + len(r_norm.as_tuple().digits) - 1
        r_mant = r_norm.scaleb(-r_exp)
        r_str  = format(r_mant.normalize(), 'f').rstrip('0').rstrip('.')
        return f"{mant_str}e{exponent:+d} + {r_str}e{r_exp:+d}"