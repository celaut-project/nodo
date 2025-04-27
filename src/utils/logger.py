import logging, os, math

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


def ssformat(number):
    """
    Smart Scientific Format.
    Formats a number in scientific notation, removing redundant zeros
    while preserving significant digits.

    Args:
        number: The number (int or float) to format.

    Returns:
        A string with the number in smart scientific notation (e.g., 9e+4, 9.0002e+4).
        Returns '0' for the number 0.
    """
    if number == 0:
        return "0" # Or you could return "0e+0" if you prefer

    # Handle negative numbers
    sign = ""
    if number < 0:
        sign = "-"
        number = abs(number)

    # Calculate the exponent (base 10)
    exponent = math.floor(math.log10(number))

    # Calculate the mantissa
    mantissa = number / (10**exponent)

    # Format the mantissa using 'g' to remove '.0' if it's an integer
    # and preserve decimals if they exist.
    mantissa_str = f"{mantissa:g}"

    # Build the final string
    # We use {exponent:+d} to ensure the sign of the exponent always appears (+4, -3)
    # If you prefer not to show the '+' for positive exponents, just use {exponent}
    result = f"{sign}{mantissa_str}e{exponent:+d}"

    return result