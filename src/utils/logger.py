import logging, os
from decimal import Decimal, getcontext, InvalidOperation

from src.utils.config import ConfigManager

env_manager = ConfigManager()
STORAGE, USE_PRINT = env_manager.get("STORAGE"), env_manager.get("USE_PRINT")

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
    # ——— 1) Convert into Decimal ———
    if isinstance(number, str):
        cleaned = number.replace('_', '').strip()
        try:
            number = Decimal(cleaned)
        except InvalidOperation:
            return str(number)
    elif not isinstance(number, Decimal):
        # ints/floats → via str() to avoid binary‑float junk
        try:
            number = Decimal(str(number))
        except InvalidOperation:
            return str(number)

    # zero shortcut
    if number == 0:
        return "0"

    # ——— 2) Bump precision to capture residual ———
    norm = number.normalize()
    total_digits = len(norm.as_tuple().digits)
    getcontext().prec = max(total_digits + 5, sig_digits + 5)

    # ——— 3) Decompose into mant_full × 10**exponent ———
    exp = norm.as_tuple().exponent
    digits = norm.as_tuple().digits
    exponent = exp + len(digits) - 1
    mant_full = number.scaleb(-exponent)

    # ——— 4) Round main mantissa ———
    quant = Decimal(1).scaleb(-(sig_digits - 1))
    mant_main = mant_full.quantize(quant)
    mant_str = format(mant_main.normalize(), 'f').rstrip('0').rstrip('.')

    # ——— 5) Compute residual ———
    residual = number - mant_main * (Decimal(10) ** exponent)
    if residual == 0:
        return f"{mant_str}e{exponent:+d}"
    else:
        r_norm = residual.normalize()
        r_exp = r_norm.as_tuple().exponent + len(r_norm.as_tuple().digits) - 1
        r_mant = r_norm.scaleb(-r_exp)
        r_str = format(r_mant.normalize(), 'f').rstrip('0').rstrip('.')
        return f"{mant_str}e{exponent:+d} + {r_str}e{r_exp:+d}"
