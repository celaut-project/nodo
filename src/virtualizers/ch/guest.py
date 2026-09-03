"""Facts about a Cloud Hypervisor guest that depend on the host architecture.

Kept in one place because there is more than one caller: execute.py builds the
kernel cmdline that launches every service, and doctor.py builds the one for its
KVM smoke test. When each spelled the answer itself, one of them was fixed and the
other was not, and the wrong console is not a cosmetic difference -- see below.

Deliberately stdlib-only, so doctor.py can import it without pulling in config or
the logger.
"""
import platform


def serial_device(machine: str = "") -> str:
    """The tty a CH guest's serial port appears as, for `console=` on the cmdline.

    Cloud Hypervisor gives aarch64 guests a PL011 and x86_64 guests an 8250, and the
    guest kernels are built to match: nodo-guest-arm64.config enables
    CONFIG_SERIAL_AMBA_PL011_CONSOLE and no 8250 driver at all, x86_64 the reverse.

    So naming the wrong one does not merely lose the serial log. On arm64 the kernel
    cannot bind a device it has no driver for, /dev/console never becomes usable, and
    `/init` -- whose first statement is `exec >/dev/console 2>&1` -- dies there. A
    non-interactive shell exits on a failed redirection, so PID 1 exits and the
    kernel panics with "Attempted to kill init! exitcode=0x00000100" at ~0.1s, having
    printed nothing of its own. Every service launch fails that way.
    """
    machine = (machine or platform.machine()).lower()
    return "ttyAMA0" if machine in ("aarch64", "arm64") else "ttyS0"
