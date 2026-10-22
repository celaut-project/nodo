import sys

from src.utils.peer_invite import InviteError, MAX_QR_BYTES, build_invite, qr_for_invite


def invite_command(argv) -> bool:
    """Print this node's invitation as a QR code, or with ``--raw <file|->`` as its bytes."""
    raw_target = None
    if argv:
        if len(argv) != 2 or argv[0] != "--raw":
            print("Usage: nodo invite [--raw <file|->]")
            return False
        raw_target = argv[1]

    from src.gateway.utils import generate_full_node_peer_info

    try:
        raw = build_invite(generate_full_node_peer_info())
    except InviteError as e:
        print(f"Cannot build the invitation: {e}")
        return False

    if raw_target is not None:
        if raw_target == "-":
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()
        else:
            with open(raw_target, "wb") as f:
                f.write(raw)
            print(f"Wrote the invitation ({len(raw)} bytes) to {raw_target}.")
        return True

    try:
        qr = qr_for_invite(raw)
    except InviteError as e:
        print(e)
        return False
    qr.terminal(compact=True)
    print(f"Invitation: {len(raw)} bytes (limit {MAX_QR_BYTES}), QR version {qr.version}, "
          f"error correction level {qr.error}, byte mode.")
    return True
